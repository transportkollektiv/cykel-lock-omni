import os
import binascii
import requests
import jsons
import time
import traceback
from datetime import datetime, timezone
from prometheus_client import Gauge
from prometheus_client.twisted import MetricsResource
from twisted.internet import protocol, reactor, endpoints
from twisted.protocols.basic import LineReceiver
from twisted.web.server import Site
from klein import Klein
from packet import Packet, Response, Command

HOST = os.environ.get('HOST', '127.0.0.1')
PORT = int(os.environ.get('PORT', '8001'))
LOCK_HOST = os.environ.get('LOCK_HOST', HOST)
LOCK_PORT = int(os.environ.get('LOCK_PORT', '9679'))
ENDPOINT = os.environ['ENDPOINT']
ENDPOINT_AUTH_HEADER = os.getenv('ENDPOINT_AUTH_HEADER', '')
LABELS = os.getenv('LABELS', None)

headers = {
    'Content-Type': 'application/json'
}
if ENDPOINT_AUTH_HEADER != '':
    headers['Authorization'] = ENDPOINT_AUTH_HEADER

promlabels = {}
if LABELS is not None:
    promlabels = dict(s.split('=') for s in LABELS.split(','))

trackervoltgauge = Gauge('tracker_battery_volts', 'tracker battery voltage', ['device_id'] + list(promlabels.keys()))
lockvoltgauge = Gauge('lock_battery_volts', 'lock battery voltage', ['device_id'] + list(promlabels.keys()))
trackertimegauge = Gauge('tracker_last_data_update', 'tracker last data timestamp', ['device_id'] + list(promlabels.keys()))
locktimegauge = Gauge('lock_last_data_update', 'lock last data timestamp', ['device_id'] + list(promlabels.keys()))

devices = dict()

# FIXME handle connection close
class OmniLockProtocol(LineReceiver):
    delimiter = b'\x0a'
    device_id = None
    device_code = None

    def __init__(self):
        self.packet = Packet()
        self.response = Response()
        self.command = Command()

    def printPacket(self, direction, packet):
        dt = datetime.utcnow().replace(tzinfo=timezone.utc).astimezone().replace(microsecond=0).isoformat()
        if direction == '>':
            direction = '==>'
        else:
            direction = '<=='
        print("%s [%s] %s %s" % (dt, self.device_id, direction, binascii.hexlify(packet), ))

    def lineReceived(self, line):
        self.printPacket("<", line)

        lbl = { 'device_id': self.device_id, **promlabels }
        locktimegauge.labels(**lbl).set(int(time.time()))

        try:
            data = self.packet.parse(line + b'\r\n')
            print(data)

            if data.imei is not None:
                self.device_id = str(data.imei)
                self.device_code = str(data.devicecode)
                devices[self.device_id] = self

            cmd = str(data.cmd)
            print(f"cmd: {cmd}")
            if cmd == 'signin':
                self.handleSignIn(data)
            elif cmd == 'heartbeat':
                self.handleHeartbeat(data)
            elif cmd == 'lock':
                self.handleLock(data)
            elif cmd == 'unlock':
                self.handleUnlock(data)
            elif cmd == 'position':
                self.handlePosition(data)
            else:
                self.handleUnknown(data)
        except Exception as e:
            print("oh, that failed:")
            print(e)
            traceback.print_exc()
            pass

    def write(self, data):
        self.printPacket(">", data)
        self.transport.write(data)

    def handleSignIn(self, data):
        print("signin from %s (%s)" % (data.imei, data.devicecode))

        update = {
            'device_id': self.device_id
        }
        print(jsons.dumps(update))
        resp = requests.post(ENDPOINT, headers=headers, data=jsons.dumps(update))
        print(resp)
        print(resp.text)

    def handleHeartbeat(self, data):
        lbl = { 'device_id': self.device_id, **promlabels }
        trackervoltgauge.labels(**lbl).set(data.data.voltage)
        lockvoltgauge.labels(**lbl).set(data.data.voltage)

        update = {
            'device_id': self.device_id,
            'battery_voltage': data.data.voltage
        }
        print(jsons.dumps(update))
        resp = requests.post(ENDPOINT, headers=headers, data=jsons.dumps(update))
        print(resp)
        print(resp.text)

    def handleLock(self, data):
        resp = self.response.build(dict(devicecode=data.devicecode, imei=data.imei, datetime=datetime.now(), data="L1"))
        self.write(resp)

    def handleUnlock(self, data):
        resp = self.response.build(dict(devicecode=data.devicecode, imei=data.imei, datetime=datetime.now(), data="L0"))
        self.write(resp)

    def handlePosition(self, data):
        resp = self.response.build(dict(devicecode=data.devicecode, imei=data.imei, datetime=datetime.now(), data="D0"))
        self.write(resp)
        self.submitLocation(data)

    def submitLocation(self, data):
        lbl = { 'device_id': self.device_id, **promlabels }
        trackertimegauge.labels(**lbl).set(int(time.time()))

        update = {
            'device_id': self.device_id
        }

        if data.data.lat and data.data.lon:
            # FIXME: something something with the data.data.lat_h, data.data.lon_h flags
            update['lat'] = data.data.lat
            update['lng'] = data.data.lon

        print(jsons.dumps(update))
        resp = requests.post(ENDPOINT, headers=headers, data=jsons.dumps(update))
        print(resp)
        print(resp.text)

    def handleUnknown(self, data):
        print(f"Got unkown packet, cmd is {data.cmd}")

    def sendUnlock(self):
        ts = datetime.now(tz=timezone.utc).timestamp()
        user = 0
        cmd = self.command.build(dict(devicecode=self.device_code, imei=self.device_id, datetime=datetime.now(), cmd=f"L0,0,{user},{ts}"))
        self.write(cmd)

    def ring(self):
        print(0) # void

    def locate(self):
        resp = self.command.build(dict(devicecode=self.device_code, imei=self.device_id, datetime=datetime.now(), cmd="D0"))
        self.write(resp)


class OmniLockProtocolFactory(protocol.Factory):
    def buildProtocol(self, addr):
        return OmniLockProtocol()

http = Klein()

class NotFound(Exception):
    pass

@http.handle_errors(NotFound)
def not_found(request, failure):
    request.setResponseCode(404)
    return 'Not found'

@http.route('/')
def home(request):
    return 'Hello world!'

@http.route('/metrics')
def metrics(request):
    return MetricsResource()

@http.route('/list')
def list(request):
    return ','.join(devices.keys())

@http.route('/<device_id>/unlock', methods=['POST'])
def lock_open(request, device_id):
    print("unlock: %s" % (device_id,))
    dev = devices.get(device_id)
    if dev is None:
        raise NotFound()
    request.setHeader('Content-Type', 'application/json')
    dev.sendUnlock()
    # FIXME: async, get confirmation from lock
    data = {"success": True, "status": "pending"}
    print(jsons.dumps(data))
    return jsons.dumps(data)

@http.route('/<device_id>/position', methods=['POST'])
def lock_position(request, device_id):
    print("position: %s" % (device_id,))
    dev = devices.get(device_id)
    if dev is None:
        raise NotFound()
    request.setHeader('Content-Type', 'application/json')
    dev.locate()
    # FIXME: async, get confirmation from lock
    data = {"success": True, "status": "pending"}
    print(jsons.dumps(data))
    return jsons.dumps(data)

@http.route('/<device_id>')
def lock(request, device_id):
    dev = devices.get(device_id)
    if dev is None:
        raise NotFound()
    return 'Hi %s!' % (device_id,)

omniendpoint = endpoints.TCP4ServerEndpoint(reactor, LOCK_PORT, interface=LOCK_HOST)
omniendpoint.listen(OmniLockProtocolFactory())
print("Listening for Lock Traffic on %s:%d" % (LOCK_HOST, LOCK_PORT))

httpendpoint = endpoints.TCP4ServerEndpoint(reactor, PORT, interface=HOST)
httpendpoint.listen(Site(http.resource()))
print("Listening for HTTP on %s:%d" % (HOST, PORT))

reactor.run()