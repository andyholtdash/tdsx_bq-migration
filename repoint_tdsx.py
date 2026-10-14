"""
Repoint Tableau packaged data sources (.tdsx) from one BigQuery dataset (schema) to another.

For every .tdsx in the input folder this does, in memory, the manual process of:
rename .tdsx -> .zip, unzip, edit the schema in the .tds XML, rezip with the .tds at the
zip root, rename .zip -> .tdsx. Originals are never modified; repointed copies are written
to the output folder under the same file name.

Only two things in the .tds are changed:
  - schema='<old>' on BigQuery <connection> elements
  - table='[<project>.<old>].[<table>]' references (main relation and object graph)
Data source names, extract file names, table names and everything else are left as they are.
Files containing custom SQL are skipped, as the schema may be inside the SQL text.

Usage (paths are relative to this script's folder unless absolute):
  python repoint_tdsx.py --dry-run      # report what would change, write nothing
  python repoint_tdsx.py                # write repointed copies + a CSV log
  python repoint_tdsx.py --input "some folder" --output "other folder" \
      --old-schema sales --new-schema dbt_production

After publishing a repointed data source to Tableau Cloud, re-embed the BigQuery
credentials and refresh the extract: the .hyper inside still holds data from the old schema.
"""

import argparse
import codecs
import csv
import os
import re
import shutil
import sys
import zipfile
from datetime import datetime
from pathlib import Path
from xml.etree import ElementTree

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_INPUT = "01. downloaded .Finance"
DEFAULT_OUTPUT = "03. converted dbt_production"
DEFAULT_OLD_SCHEMA = "finance"
DEFAULT_NEW_SCHEMA = "dbt_production"

CONNECTION_TAG = re.compile(r"<connection\b[^>]*>")
BIGQUERY_CLASS = re.compile(r"""\bclass=(['"])bigquery\1""")
CUSTOM_SQL = re.compile(r"""<relation\b[^>]*\btype=(['"])text\1""")
COPY_CHUNK = 1024 * 1024


class RepointError(Exception):
    """The file can't be repointed safely; nothing is written for it."""


class SkipFile(Exception):
    """The file is deliberately left alone."""


def schema_attr_pattern(schema):
    return re.compile(r"""\bschema=(['"])""" + re.escape(schema) + r"\1")


def table_ref_pattern(schema):
    # table='[project.schema].[table]' -> 1: "table='[project."  3: "].[table]"  4: table name
    return re.compile(r"""(\btable=(['"])\[[^\]\[]*?\.)""" + re.escape(schema) + r"""(\]\.\[([^\]]*)\])""")


def repoint_tds(text, old, new):
    """Return (edited text, schema edits, table edits, table names) for a .tds document."""
    if CUSTOM_SQL.search(text):
        raise SkipFile("custom SQL found - repoint this one by hand")

    schema_attr = schema_attr_pattern(old)
    schema_edits = 0

    def fix_connection(match):
        nonlocal schema_edits
        tag = match.group(0)
        if not BIGQUERY_CLASS.search(tag):
            return tag
        tag, count = schema_attr.subn(lambda a: f"schema={a.group(1)}{new}{a.group(1)}", tag)
        schema_edits += count
        return tag

    text = CONNECTION_TAG.sub(fix_connection, text)

    table_ref = table_ref_pattern(old)
    tables = sorted({m.group(4) for m in table_ref.finditer(text)})
    text, table_edits = table_ref.subn(lambda m: m.group(1) + new + m.group(3), text)

    if schema_edits == 0:
        raise RepointError(f"schema '{old}' not found on any BigQuery connection")
    if table_edits == 0:
        raise RepointError(f"no table references to '{old}' found")

    # Anything still pointing at the old schema means the file has a shape this script doesn't handle.
    leftover = schema_attr.search(text) or re.search(r"[\[.]" + re.escape(old) + r"\]", text)
    if leftover:
        raise RepointError(f"'{old}' still referenced after edit: {leftover.group(0)}")

    return text, schema_edits, table_edits, tables


def write_tdsx(zin, tds_info, tds_bytes, out_path):
    """Copy every entry of zin to out_path, swapping in the edited .tds. Written via a .partial file."""
    partial = out_path.with_name(out_path.name + ".partial")
    try:
        with zipfile.ZipFile(partial, "w", zipfile.ZIP_DEFLATED) as zout:
            for info in zin.infolist():
                copy_info = zipfile.ZipInfo(info.filename, date_time=info.date_time)
                copy_info.compress_type = info.compress_type
                copy_info.external_attr = info.external_attr
                if info.filename == tds_info.filename:
                    zout.writestr(copy_info, tds_bytes)
                elif info.is_dir():
                    zout.writestr(copy_info, b"")
                else:
                    # Known size up front lets zipfile switch to ZIP64 for very large extracts.
                    copy_info.file_size = info.file_size
                    with zin.open(info) as src, zout.open(copy_info, "w") as dst:
                        shutil.copyfileobj(src, dst, COPY_CHUNK)
        verify_tdsx(partial, zin.namelist(), tds_info.filename, tds_bytes)
        os.replace(partial, out_path)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise


def verify_tdsx(path, expected_names, tds_name, tds_bytes):
    with zipfile.ZipFile(path) as z:
        bad = z.testzip()
        if bad:
            raise RepointError(f"CRC check failed for {bad} in the written file")
        if z.namelist() != expected_names:
            raise RepointError("written file has different entries from the source")
        if z.read(tds_name) != tds_bytes:
            raise RepointError(".tds in the written file doesn't match the edited version")


def process(src_path, out_dir, old, new, dry_run, overwrite):
    out_path = out_dir / src_path.name
    row = {"file": src_path.name, "status": "", "tables": "", "schema_edits": 0, "table_edits": 0,
           "message": "", "source": str(src_path), "output": ""}
    try:
        exists = out_path.exists() and not overwrite
        if exists and not dry_run:
            raise SkipFile("output already exists (use --overwrite to replace it)")

        with zipfile.ZipFile(src_path) as zin:
            tds_entries = [i for i in zin.infolist()
                           if "/" not in i.filename and i.filename.lower().endswith(".tds")]
            if len(tds_entries) != 1:
                raise RepointError(f"expected one .tds at the zip root, found {len(tds_entries)}")
            tds_info = tds_entries[0]

            raw = zin.read(tds_info)
            bom = codecs.BOM_UTF8 if raw.startswith(codecs.BOM_UTF8) else b""
            text, schema_edits, table_edits, tables = repoint_tds(raw[len(bom):].decode("utf-8"), old, new)
            tds_bytes = bom + text.encode("utf-8")
            try:
                ElementTree.fromstring(tds_bytes)
            except ElementTree.ParseError as e:
                raise RepointError(f"edited .tds is not valid XML: {e}")

            row.update(tables=", ".join(tables), schema_edits=schema_edits, table_edits=table_edits)
            if dry_run:
                row["status"] = "OK (dry run)"
                if exists:
                    row["message"] = "output already exists - a real run would skip it"
                return row

            write_tdsx(zin, tds_info, tds_bytes, out_path)
        row.update(status="OK", output=str(out_path))
    except SkipFile as e:
        row.update(status="SKIPPED", message=str(e))
    except RepointError as e:
        row.update(status="FAILED", message=str(e))
    except (zipfile.BadZipFile, UnicodeDecodeError, OSError) as e:
        row.update(status="FAILED", message=f"{type(e).__name__}: {e}")
    return row


def resolve(path_text):
    path = Path(path_text)
    return (path if path.is_absolute() else SCRIPT_DIR / path).resolve()


def parse_args():
    parser = argparse.ArgumentParser(description="Repoint .tdsx files from one BigQuery schema to another.")
    parser.add_argument("--input", default=DEFAULT_INPUT, help=f"folder of .tdsx files (default: {DEFAULT_INPUT})")
    parser.add_argument("--output", default=DEFAULT_OUTPUT, help=f"folder for repointed copies (default: {DEFAULT_OUTPUT})")
    parser.add_argument("--old-schema", default=DEFAULT_OLD_SCHEMA, help=f"default: {DEFAULT_OLD_SCHEMA}")
    parser.add_argument("--new-schema", default=DEFAULT_NEW_SCHEMA, help=f"default: {DEFAULT_NEW_SCHEMA}")
    parser.add_argument("--dry-run", action="store_true", help="report what would change, write nothing")
    parser.add_argument("--overwrite", action="store_true", help="replace files already in the output folder")
    return parser.parse_args()


def main():
    args = parse_args()
    in_dir, out_dir = resolve(args.input), resolve(args.output)
    if not in_dir.is_dir():
        sys.exit(f"Input folder not found: {in_dir}")
    if os.path.normcase(in_dir) == os.path.normcase(out_dir):
        sys.exit("Input and output folders must be different, so the originals are never overwritten.")

    sources = sorted((p for p in in_dir.iterdir() if p.is_file() and p.suffix.lower() == ".tdsx"),
                     key=lambda p: p.name.lower())
    if not sources:
        sys.exit(f"No .tdsx files found in {in_dir}")
    if not args.dry_run:
        out_dir.mkdir(parents=True, exist_ok=True)

    print(f"Schema:  {args.old_schema} -> {args.new_schema}{'   (DRY RUN - nothing is written)' if args.dry_run else ''}")
    print(f"Input:   {in_dir}")
    print(f"Output:  {out_dir}\n")
    width = max(len(p.name) for p in sources)
    print(f"{'STATUS':<13} {'FILE':<{width}}  {'SCHEMA':>6} {'TABLE':>5}  TABLE NAME / MESSAGE")

    rows = []
    for src in sources:
        row = process(src, out_dir, args.old_schema, args.new_schema, args.dry_run, args.overwrite)
        rows.append(row)
        detail = "  ".join(x for x in (row["tables"], row["message"]) if x)
        print(f"{row['status']:<13} {row['file']:<{width}}  {row['schema_edits']:>6} {row['table_edits']:>5}  {detail}",
              flush=True)

    counts = {s: sum(r["status"].startswith(s) for r in rows) for s in ("OK", "SKIPPED", "FAILED")}
    print(f"\n{counts['OK']} OK, {counts['SKIPPED']} skipped, {counts['FAILED']} failed")

    if not args.dry_run:
        log_path = out_dir / f"repoint_log_{datetime.now():%Y%m%d-%H%M%S}.csv"
        with open(log_path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=["file", "status", "tables", "schema_edits", "table_edits",
                                                   "message", "source", "output"])
            writer.writeheader()
            for row in rows:
                writer.writerow(row)
        print(f"Log:     {log_path}")

    return 1 if counts["FAILED"] else 0


if __name__ == "__main__":
    sys.exit(main())
