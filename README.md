# PhoneCheck data pipeline (set up once, then it runs by itself)

Every month GitHub downloads the latest Osmocom and community TAC data, rebuilds the database, runs safety
checks and publishes it. The PhoneCheck app checks for it when opened and updates itself.

## One-time setup (about 10 minutes)
1. Create a **public** GitHub repository, for example `phonecheck-data`.
2. Upload everything in this folder to it, including the hidden `.github` folder
   (GitHub web: Add file > Upload files; or use git).
3. Open the repository's **Actions** tab, enable workflows if asked, choose **Update phone data** and click
   **Run workflow**. After a few minutes there is a release named **Phone data (automatic)**.
4. Your app's update address is:
   `https://github.com/YOUR_USER/phonecheck-data/releases/download/data/manifest.json`
   Open it in a browser to check that it shows version and counts.
5. Build the app with that address:
   `flutter build appbundle --dart-define=DATA_MANIFEST_URL=<the address above>`

After that you do nothing. The workflow runs monthly and the app updates itself.

## What protects users from bad data
The release is **not published** if any of these fail, and the app just keeps its current data:
database integrity, fewer than 50,000 TAC entries, a drop of more than 10% against the last release,
missing Samsung/Apple data, an unreadable source file, or a file over 60 MB. If nothing changed since last time,
nothing is published. GitHub emails you when a run fails.

## What can still need you
* **A failure email:** a source file moved or changed format. Forward it to me and I will adjust the converter.
* **A source stops updating:** the build keeps succeeding but adds nothing new. Check once a quarter that the
  release's `counts.tac` is still growing.
* **Scheduled runs pause** after 60 days without repository activity. The workflow commits `last_run.txt` each month
  to avoid this. If GitHub ever pauses it, click **Enable workflow**.
* **Licenses:** the data is CC BY-SA 3.0 (from Osmocom). This repository is public, which satisfies share-alike.
  Keep `NOTICE.md` in the release.

## Run the checks by hand
`python3 publish_manifest.py --tacdb tacdb.json --community-csv tac_full.csv --repo YOUR_USER/phonecheck-data --out dist`
