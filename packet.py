import binascii
import datetime

from construct import (
    Adapter,
    Byte,
    Bytes,
    Const,
    Enum,
    FixedSized,
    Flag,
    GreedyBytes,
    GreedyString,
    Hex,
    MappingError,
    NullTerminated,
    Struct,
    Switch,
)


class HexString(Hex):
    def _decode(self, obj, context, path):
        if isinstance(obj, bytes):
            return HexDisplayedString(obj)
        return super._decode(obj, context, path)


class HexDisplayedString(bytes):
    def __str__(self):
        return binascii.hexlify(self).decode("ascii")

    def __repr__(self):
        return self.__str__()


class CommaTerminated(NullTerminated):
    def __init__(self, subcon, require=True):
        super(CommaTerminated, self).__init__(
            subcon, term=b",", include=False, consume=True, require=require
        )


class DateTimeAdapter(Adapter):
    def _decode(self, obj, context, path):
        if obj.year == "00" and obj.month == "00" and obj.day == "00":
            return None
        century = datetime.datetime.now().year // 100
        year = century * 100 + int(obj.year)
        return datetime.datetime(
            year,
            int(obj.month),
            int(obj.day),
            hour=int(obj.hour),
            minute=int(obj.minute),
            second=int(obj.second),
        )

    def _encode(self, obj, context, path):
        if not isinstance(obj, datetime.datetime):
            raise MappingError("cannot convert %r into datetime" % (obj,))
        d = dict(
            year=str(obj.year)[2:],
            month=str(obj.month),
            day=str(obj.day),
            hour=str(obj.hour),
            minute=str(obj.minute),
            second=str(obj.second),
        )
        for k in d:
            if len(d[k]) == 1:
                d[k] = f"0{d[k]}"
        return d


class IntegerStringAdapter(Adapter):
    def _decode(self, obj, context, path):
        return int(obj)


class VoltageStringAdapter(Adapter):
    def _decode(self, obj, context, path):
        return int(obj) / 100


class MayBeNoneAdapter(Adapter):
    def _decode(self, obj, context, path):
        if obj == b"":
            return None
        return obj


class Packet:
    dt = Struct(
        # FIXME: integer, padded with leading zeros.
        "year" / FixedSized(2, GreedyString("ascii")),
        "month" / FixedSized(2, GreedyString("ascii")),
        "day" / FixedSized(2, GreedyString("ascii")),
        "hour" / FixedSized(2, GreedyString("ascii")),
        "minute" / FixedSized(2, GreedyString("ascii")),
        "second" / FixedSized(2, GreedyString("ascii")),
    )

    signin = Struct(
        "voltage" / VoltageStringAdapter(GreedyBytes),
    )

    heartbeat = Struct(
        "locked" / Enum(CommaTerminated(Flag)),
        "voltage" / CommaTerminated(VoltageStringAdapter(GreedyBytes)),
        "gsmsignal" / IntegerStringAdapter(GreedyBytes),
    )

    lock_status = Struct(
        "voltage" / CommaTerminated(VoltageStringAdapter(GreedyBytes)),
        "gsmsignal" / CommaTerminated(GreedyBytes),
        "reserved" / CommaTerminated(GreedyBytes),
        "locked" / Enum(CommaTerminated(Byte), unlocked=0, locked=1),
        "reserved" / GreedyBytes,
    )

    ringing = Struct(
        "seconds" / CommaTerminated(GreedyBytes),
        "reserved" / GreedyBytes,
    )

    version = Struct(
        "version" / CommaTerminated(GreedyString("ascii")),
        "compiletime" / GreedyString("ascii"),
    )

    lock = Struct(
        "userid" / CommaTerminated(GreedyBytes),
        "unlocked_at" / CommaTerminated(GreedyBytes),
        "riding_time" / GreedyBytes,  # minutes
    )

    unlock = Struct(
        "locked"
        / Enum(CommaTerminated(GreedyString("ascii")), unlocked=b"0", locked=b"1"),
        "userid" / CommaTerminated(GreedyBytes),
        "unlocked_at" / GreedyBytes,
    )

    position = Struct(
        Const(b"0,"),
        "time" / CommaTerminated(GreedyBytes),
        "status" / Enum(CommaTerminated(GreedyBytes), invalid=b"V", active=b"A"),
        "lat" / MayBeNoneAdapter(CommaTerminated(GreedyBytes)),
        "lat_h"
        / Enum(CommaTerminated(GreedyBytes), invalid=b"", north=b"N", south=b"S"),
        "lon" / MayBeNoneAdapter(CommaTerminated(GreedyBytes)),
        "lon_h" / Enum(CommaTerminated(GreedyBytes), invalid=b"", east=b"E", west=b"W"),
        "ground_rate" / MayBeNoneAdapter(CommaTerminated(GreedyBytes)),
        "heading" / MayBeNoneAdapter(CommaTerminated(GreedyBytes)),
        "date" / MayBeNoneAdapter(CommaTerminated(GreedyBytes)),
        "mag_degrees" / MayBeNoneAdapter(CommaTerminated(GreedyBytes)),
        "mag_direction" / MayBeNoneAdapter(CommaTerminated(GreedyBytes)),
        "mode"
        / Enum(
            GreedyBytes, automatic=b"A", differential=b"D", estimation=b"E", invalid=b"N"
        ),
    )

    def __init__(self):
        self.protocol = Struct(
            Const(b"*CMDR,"),
            "devicecode" / CommaTerminated(GreedyString("ascii")),
            "imei" / CommaTerminated(GreedyString("ascii")),
            "datetime" / CommaTerminated(DateTimeAdapter(self.dt)),
            "cmd"
            / Enum(
                Bytes(2),
                signin=b"Q0",
                heartbeat=b"H0",
                lock_status=b"S5",
                ringing=b"S8",
                lock=b"L1",
                unlock=b"L0",
                version=b"G0",
                position=b"D0",
                update=b"U0",
            ),
            Const(b","),
            "data"
            / NullTerminated(
                Switch(
                    lambda this: this.cmd,
                    {
                        "signin": self.signin,
                        "heartbeat": self.heartbeat,
                        "lock_status": self.lock_status,
                        "ringing": self.ringing,
                        "lock": self.lock,
                        "unlock": self.unlock,
                        "version": self.version,
                        "position": self.position,
                    },
                    default=GreedyBytes
                ),
                term=b"#",
            ),
        )

    def parse(self, packet):
        return self.protocol.parse(packet)


class Command:
    dt = Struct(
        "year" / FixedSized(2, GreedyString("ascii")),
        "month" / FixedSized(2, GreedyString("ascii")),
        "day" / FixedSized(2, GreedyString("ascii")),
        "hour" / FixedSized(2, GreedyString("ascii")),
        "minute" / FixedSized(2, GreedyString("ascii")),
        "second" / FixedSized(2, GreedyString("ascii")),
    )

    def __init__(self):
        self.protocol = Struct(
            Const(b"\xFF\xFF*CMDS,"),
            "devicecode" / CommaTerminated(GreedyString("ascii")),
            "imei" / CommaTerminated(GreedyString("ascii")),
            "datetime" / CommaTerminated(DateTimeAdapter(self.dt)),
            "cmd" / NullTerminated(GreedyString("ascii"), term=b"#"),
        )

    def build(self, packetdata):
        return self.protocol.build(packetdata)


class Response:
    dt = Struct(
        "year" / FixedSized(2, GreedyString("ascii")),
        "month" / FixedSized(2, GreedyString("ascii")),
        "day" / FixedSized(2, GreedyString("ascii")),
        "hour" / FixedSized(2, GreedyString("ascii")),
        "minute" / FixedSized(2, GreedyString("ascii")),
        "second" / FixedSized(2, GreedyString("ascii")),
    )

    def __init__(self):
        self.protocol = Struct(
            Const(b"\xFF\xFF*CMDS,"),
            "devicecode" / CommaTerminated(GreedyString("ascii")),
            "imei" / CommaTerminated(GreedyString("ascii")),
            "datetime" / CommaTerminated(DateTimeAdapter(self.dt)),
            Const(b"Re"),
            Const(b","),
            "data" / NullTerminated(GreedyString("ascii"), term=b"#"),
        )

    def build(self, packetdata):
        return self.protocol.build(packetdata)
