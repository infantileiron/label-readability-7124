import csv
import io


def pairing_csv(export):
    output = io.StringIO(newline='')
    writer = csv.writer(output)
    writer.writerow(['run_id', 'checksum', 'revision', 'visitor_code', 'reading_order', 'old_ok', 'new_ok', 'complete'])
    for row in export['snapshot']['pairs']:
        old, new = row['old_ok'], row['new_ok']
        writer.writerow([export['id'], export['checksum'], export['revision'], row['visitor_code'],
                         row['reading_order'], old, new, old is not None and new is not None])
    return output.getvalue()


def roster_csv(roster):
    """One row per member, carrying the batch and the run it was drawn from."""
    output = io.StringIO(newline='')
    writer = csv.writer(output)
    writer.writerow(['roster_id', 'checksum', 'study_code', 'study_id', 'run_id', 'run_revision',
                     'reading_order', 'visitor_code'])
    for row in roster['members']:
        item = next(entry for entry in roster['items'] if entry['study_id'] == row['study_id'])
        writer.writerow([roster['id'], roster['checksum'], row['study_code'], row['study_id'],
                         row['run_id'], item['run_revision'], row['reading_order'],
                         row['visitor_code']])
    return output.getvalue()
