# repoint_tdsx

Repoints Tableau packaged data sources (.tdsx) from one BigQuery dataset to another by editing the XML inside each file. Use it when tables have moved to a new dataset (for example `finance` to `dbt_production`) with the same names, and you don't want to rebuild the data sources in Tableau.

## What it does

A .tdsx is a zip holding a .tds (XML) and usually a .hyper extract. For each .tdsx in the input folder, the script:

1. Reads the .tds from the zip, without extracting anything to disk.
2. Changes `schema='<old>'` on BigQuery connections, and `[<project>.<old>].[<table>]` table references, to the new dataset.
3. Writes a new .tdsx with the same name to the output folder. Every other entry, including the extract, is copied unchanged.
4. Reopens the new file and checks the CRCs, the entry list and the edited .tds.

Nothing else in the .tds changes. Data source names, extract file names and table names are left alone, even if they contain the old dataset name.

## Requirements

Python 3.8 or later. Standard library only, so there's nothing to install.

## Usage

```
python repoint_tdsx.py --dry-run
python repoint_tdsx.py
```

| Option | Default | Meaning |
|---|---|---|
| `--input` | `01. downloaded .Finance` | Folder of .tdsx files to convert |
| `--output` | `03. converted dbt_production` | Folder for the repointed copies |
| `--old-schema` | `finance` | Dataset to move away from |
| `--new-schema` | `dbt_production` | Dataset to point at |
| `--dry-run` | off | Report what would change and write nothing |
| `--overwrite` | off | Replace files already in the output folder |

Relative paths resolve against the script's folder, not the current directory. The defaults are constants at the top of the script.

For another dataset:

```
python repoint_tdsx.py --input "downloaded sales" --output "converted sales" --old-schema sales --new-schema dbt_production
```

The script prints one line per file showing OK, SKIPPED or FAILED, the table name and the number of edits. It also writes the same rows to `repoint_log_<timestamp>.csv` in the output folder. The exit code is 1 if any file failed.

## Safety

- Originals are never modified, and the input and output folders must be different.
- Existing output files are skipped unless you pass `--overwrite`.
- Files with custom SQL are skipped, because the dataset name may be inside the SQL. Repoint those by hand.
- A file fails, and nothing is written for it, if the old dataset isn't found, if any reference to it is left after the edit, or if the edited XML doesn't parse.
- Output goes to a `.partial` file first and is renamed only once it passes the checks.

## After publishing

The .tdsx doesn't carry database credentials, so embed the BigQuery credentials again when you publish each data source. Then refresh the extract. Until you do, the .hyper still holds data from the old dataset.

## Limitations

- BigQuery connections only.
- Changes the dataset, not table or column names. If a dbt model has been renamed, edit that .tds by hand.
- Expects one .tds at the root of each .tdsx. Workbooks (.twbx) aren't handled.

## Editing a .tdsx by hand

If you do it manually (rename to .zip, unzip, edit, rezip), zip the .tds and the `Data` folder themselves, not the folder that contains them. If the .tds isn't at the root of the zip, Tableau fails with "An unexpected error occurred opening the packaged workbook" (error code A19C3D8C).
