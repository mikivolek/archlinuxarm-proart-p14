#!/usr/bin/env python3
"""Extract exact exported symbol CRCs from a little-endian AArch64 ELF module."""
import json
import pathlib
import struct
import sys


def exports(path):
    blob = pathlib.Path(path).read_bytes()
    assert blob[:6] == b"\x7fELF\x02\x01", "Expected ELF64 little endian"
    header = struct.unpack_from("<16sHHIQQQIHHHHHH", blob)
    assert header[2] == 183, "Expected AArch64"
    section_offset, section_size, count, strings_index = header[6], header[11], header[12], header[13]
    sections = [struct.unpack_from("<IIQQQQIIQQ", blob, section_offset + i * section_size) for i in range(count)]

    def data(section):
        return blob[section[4]: section[4] + section[5]]

    def string(table, offset):
        return table[offset:table.index(b"\0", offset)].decode()

    names = data(sections[strings_index])
    rows = {}
    for section in sections:
        if section[1] != 2:
            continue
        table, strings = data(section), data(sections[section[6]])
        for offset in range(0, len(table), section[9]):
            name_at, _, _, index, value, _ = struct.unpack_from("<IBBHQQ", table, offset)
            name = string(strings, name_at)
            if not name.startswith("__crc_"):
                continue
            assert index < len(sections), "Expected relative CRC section"
            target = sections[index]
            target_name = string(names, target[0])
            assert target_name in ("___kcrctab", "___kcrctab_gpl", "__kcrctab", "__kcrctab_gpl"), target_name
            crc = struct.unpack_from("<I", data(target), value)[0]
            rows[name[6:]] = {"crc": f"0x{crc:08x}", "export": "EXPORT_SYMBOL_GPL" if target_name.endswith("_gpl") else "EXPORT_SYMBOL"}
    assert rows, "No exported CRCs"
    return rows


if __name__ == "__main__":
    rows = exports(sys.argv[1])
    if len(sys.argv) > 2 and sys.argv[2] == "--symvers":
        for name, row in sorted(rows.items()):
            print(f'{row["crc"]}\t{name}\tnvidia\t{row["export"]}\t')
    else:
        print(json.dumps(rows, indent=2, sort_keys=True))
