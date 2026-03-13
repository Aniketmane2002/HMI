import sys
import time
import can

CHANNEL  = 0
BITRATE  = 250000
CAN_ID   = 0x20

# -----------------------------------------------------------------------
# Signed byte (int8) limits — the max range a single CAN byte can carry
# when interpreted as two's-complement signed 8-bit integer:
#
#   Bit pattern  Unsigned  Signed
#   0x00         0         0       <- zero
#   0x01         1        +1
#   ...
#   0x7F         127      +127     <- MAX POSITIVE (int8 ceiling)
#   0x80         128      -128     <- MAX NEGATIVE (int8 floor)
#   0x81         129      -127
#   ...
#   0xFF         255       -1
#
# So:  MIN_NM = -128,  MAX_NM = +127
# -----------------------------------------------------------------------
MIN_NM = -128          # int8 minimum  (0x80)
MAX_NM = +127          # int8 maximum  (0x7F)
STEP   = 1             # 1 Nm step fits neatly in the 255-count range
PERIOD_S = 0.010       # 10 ms transmit period (matches GUI requirement)


def encode_int8(value: int) -> int:
    """
    Encode a signed Nm value (-128..+127) as a single raw CAN byte (0x00..0xFF).

    Two's-complement encoding:
        positive / zero  ->  value unchanged        (0x00..0x7F)
        negative         ->  value + 256             (0x80..0xFF)

    Examples:
        encode_int8(   0) -> 0x00
        encode_int8( +127) -> 0x7F   (max positive)
        encode_int8( -128) -> 0x80   (max negative)
        encode_int8(  -1) -> 0xFF
    """
    if not (MIN_NM <= value <= MAX_NM):
        raise ValueError(f"value {value} out of int8 range [{MIN_NM}, {MAX_NM}]")
    return value if value >= 0 else (value + 256)


def pack_int8_4wheel(fl: int, fr: int, rl: int, rr: int) -> bytes:
    """
    Pack four wheel torques into exactly 4 bytes.
    Each wheel occupies one byte encoded as signed int8.

    Layout:
        Byte 0 = Front Left   (int8)
        Byte 1 = Front Right  (int8)
        Byte 2 = Rear  Left   (int8)
        Byte 3 = Rear  Right  (int8)

    Range per wheel: -128 Nm (0x80) to +127 Nm (0x7F)
    """
    return bytes([
        encode_int8(fl),
        encode_int8(fr),
        encode_int8(rl),
        encode_int8(rr),
    ])


def ramp_sequence():
    """
    Infinite generator: 0 -> +127 -> 0 -> -128 -> 0, repeating.

    Positive ramp :  0,  1,  2, ...  127    (step +1)
    Back to zero  :  0
    Negative ramp :  0, -1, -2, ... -128    (step -1)
    Back to zero  :  0
    Then repeats.
    """
    while True:
        # 0 -> +127
        v = 0
        while v <= MAX_NM:
            yield v
            v += STEP

        yield 0  # return to zero

        # 0 -> -128
        v = 0
        while v >= MIN_NM:
            yield v
            v -= STEP

        yield 0  # return to zero


def main():
    # --- Open Vector bus ---
    try:
        bus = can.interface.Bus(
            interface="vector",
            channel=CHANNEL,
            bitrate=BITRATE,
        )
    except Exception as e:
        print(f"[ERROR] Failed to open Vector bus: {e}", file=sys.stderr)
        sys.exit(2)

    print(f"TX  Vector channel={CHANNEL}  bitrate={BITRATE} bit/s  ID=0x{CAN_ID:03X}")
    print(f"Format : 4 bytes, one signed int8 per wheel")
    print(f"Range  : {MIN_NM} Nm (0x{encode_int8(MIN_NM):02X}) to "
          f"+{MAX_NM} Nm (0x{encode_int8(MAX_NM):02X})")
    print(f"Period : {int(PERIOD_S * 1000)} ms    Ctrl+C to stop.\n")

    # Pre-allocate message; update .data each tick
    msg = can.Message(arbitration_id=CAN_ID, is_extended_id=False, data=bytes(4))

    seq = ramp_sequence()
    next_deadline = time.monotonic()

    try:
        while True:
            torque = next(seq)          # same value on all four wheels
            data = pack_int8_4wheel(torque, torque, torque, torque)
            msg.data = data

            try:
                bus.send(msg)
                raw = list(data)
                print(
                    f"0x{CAN_ID:03X}  "
                    f"[FL:0x{raw[0]:02X}] [FR:0x{raw[1]:02X}] "
                    f"[RL:0x{raw[2]:02X}] [RR:0x{raw[3]:02X}]"
                    f"   FL=FR=RL=RR={torque:+4d} Nm"
                )
            except can.CanError as ce:
                print(f"[ERROR] CAN send failed: {ce}", file=sys.stderr)

            # Pace at exactly PERIOD_S (drift-free)
            next_deadline += PERIOD_S
            sleep_time = next_deadline - time.monotonic()
            if sleep_time > 0:
                time.sleep(sleep_time)
            else:
                next_deadline = time.monotonic()   # reset if late

    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        try:
            bus.shutdown()
        except Exception:
            pass


if __name__ == "__main__":
    main()
