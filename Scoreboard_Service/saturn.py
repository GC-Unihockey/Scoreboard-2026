STX = 0x02
ETX = 0x03

def calc_crc_over_payload(payload: bytes) -> int:
    crc = STX ^ ETX
    for b in payload:
        crc ^= b
    return crc

def make_frame(payload: bytes) -> bytes:
    crc = calc_crc_over_payload(payload)
    return bytes([STX]) + payload + bytes([ETX, crc])

def extract_frames(buffer: bytearray):
    frames = []
    while True:
        try:
            stx = buffer.index(STX)
        except ValueError:
            buffer.clear()
            break

        if stx > 0:
            del buffer[:stx]

        if len(buffer) < 4:
            break

        try:
            etx = buffer.index(ETX, 1)
        except ValueError:
            break

        if etx + 1 >= len(buffer):
            break

        payload = bytes(buffer[1:etx])
        crc_rx = buffer[etx + 1]
        del buffer[:etx + 2]

        if calc_crc_over_payload(payload) != crc_rx:
            continue

        frames.append(payload)

    return frames
