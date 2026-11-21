"""Aggregate attachment metadata without printing names, mail text, or binary data."""
import collections
import json
from pathlib import Path

import pypff
from pcb_assistant.ingestion.mail import iter_messages, safe_property, write_json

ROOT = Path(__file__).resolve().parents[2]
KNOWN = set('pdf xls xlsx xlsm doc docx ppt pptx txt csv rtf html htm jpg jpeg png gif bmp tif tiff zip rar 7z msg eml dwg dxf gbr gerber exe'.split())


def main():
    counts, extensions, sizes, scan = (collections.Counter() for _ in range(4))
    output = ROOT / 'data/attachment_inventory.json'
    pst = pypff.file()
    pst.open(str(ROOT / '19924784-20240420.pst'), 'r')
    try:
        for message in iter_messages(pst.root_folder, scan):
            counts['messages'] += 1
            try:
                count = message.number_of_attachments
                counts['listed_attachments'] += count
                for number in range(count):
                    try:
                        attachment = message.get_attachment(number)
                        name = str(safe_property(attachment, 'long_filename', '') or '')
                        suffix = Path(name).suffix.lower().lstrip('.')
                        category = suffix if suffix in KNOWN else ('no_extension' if not suffix else 'other')
                        size = attachment.size
                        extensions[category] += 1
                        sizes[category] += size
                        counts['readable_attachments'] += 1
                        counts['total_reported_bytes'] += size
                        counts['largest_bytes'] = max(counts['largest_bytes'], size)
                    except Exception:
                        counts['attachment_metadata_errors'] += 1
            except Exception:
                counts['message_attachment_errors'] += 1
            if counts['messages'] % 1000 == 0:
                print(json.dumps({'event': 'attachment_inventory', **counts}), flush=True)
                write_json(output, {'status': 'scanning', 'counts': dict(counts), 'by_extension': dict(extensions), 'bytes_by_extension': dict(sizes)})
    finally:
        pst.close()
    result = {'status': 'completed', 'counts': dict(counts), 'by_extension': dict(extensions), 'bytes_by_extension': dict(sizes), 'scan': dict(scan)}
    write_json(output, result)
    print(json.dumps({'event': 'attachment_inventory_complete', **result}), flush=True)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(json.dumps({'event': 'inventory_failed', 'error_type': type(exc).__name__}), flush=True)
        raise SystemExit(1)
