
import sys
import time
import struct
import can


CHANNEL  = 0            # Vector channel index
BITRATE  = 250000       
CAN_ID   =0X20  


MIN_NM = -1400
MAX_NM =  1400
STEP   = 5
PERIOD_S = 0.0010      # 100 ms

def pack_le_s16(vals):
    """
    Pack four signed 16-bit integers into 8 bytes, LITTLE-ENDIAN.
    Layout matches your GUI:
      [FL_lo, FL_hi, FR_lo, FR_hi, RL_lo, RL_hi, RR_lo, RR_hi]
    """
    for v in vals:
        if not (-32768 <= v <= 32767):
            raise ValueError(f"value {v} not int16")
        if not (MIN_NM <= v <= MAX_NM):
            raise ValueError(f"value {v} out of range [{MIN_NM}, {MAX_NM}]")
    return struct.pack("<hhhh", *vals)

def ramp_sequence():
    """
    Infinite generator producing:
      0 -> +1400 (step +5), then 0 -> -1400 (step -5), repeating.
    Each value is yielded once per call.
    """
    # 0 -> +1400
    v = 0
    while v <= MAX_NM:
        yield v
        v += STEP
    # back to 0 (one shot)
    v = 0
    yield v
    # 0 -> -1400
    v = 0
    while v >= MIN_NM:
        yield v
        v -= STEP
    # back to 0 (one shot)
    yield 0
    # loop repeats by recursion of the caller
    # (the caller will re-invoke this function when it exhausts)

def seq_infinite():
    """Wrap ramp_sequence() so it runs forever."""
    while True:
        for v in ramp_sequence():
            yield v

def main():
    # --- Open Vector bus ---
    try:
        bus = can.interface.Bus(
                        interface="vector",  
                        channel=0,                    
                        bitrate=250000               
                       
                    )
    except Exception as e:
        print(f"[ERROR] Failed to open Vector bus: {e}", file=sys.stderr)
        sys.exit(2)

    print(f"TX on Vector channel={CHANNEL}, bitrate={BITRATE} bit/s, ID=0x{CAN_ID:03X}")
    print("Format: LITTLE-ENDIAN signed int16 per wheel. Period = 100 ms. Ctrl+C to stop.")

    # Create message object once; update .data each tick
    msg = can.Message(arbitration_id=CAN_ID, is_extended_id=True, data=bytes(8))

    seq = seq_infinite()
    next_deadline = time.monotonic()

    try:
        while True:
            torque = next(seq)  # same value for all four wheels
            data = pack_le_s16((torque, torque, torque, torque))
            msg.data = data

            try:
                bus.send(msg)
                b = list(data)
                print(
                    f"0x{CAN_ID:03X} "
                    f"[FL:{b[0]:02X} {b[1]:02X}] [FR:{b[2]:02X} {b[3]:02X}] "
                    f"[RL:{b[4]:02X} {b[5]:02X}] [RR:{b[6]:02X} {b[7]:02X}]  |  "
                    f"FL=FR=RL=RR={torque:+d} Nm"
                )
            except can.CanError as ce:
                print(f"[ERROR] CAN send failed: {ce}", file=sys.stderr)

            # pace at exactly 100 ms (best-effort)
            next_deadline += PERIOD_S
            sleep_time = next_deadline - time.monotonic()
            if sleep_time > 0:
                time.sleep(sleep_time)
            else:
                # If we're late, reset the schedule to now to avoid drift
                next_deadline = time.monotonic()
                # Optional: print a warning if consistently late

    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        try:
            bus.shutdown()
        except Exception:
            pass

if __name__ == "__main__":
    main()
