# Publish ICS via GitHub Pages

## 1) Push this repository to GitHub
- Ensure `convert_table_to_ics.py`, `requirements.txt`, `output/tables/table_03.csv`, and `.github/workflows/publish-ics-pages.yml` are in the repo.

## 2) Enable GitHub Pages
- Open: `Settings` -> `Pages`
- In `Build and deployment`, set `Source` to `GitHub Actions`.

## 3) Run the workflow
- Open: `Actions` -> `Publish ICS to GitHub Pages`
- Click `Run workflow`
- Enter:
  - `first_semester_start`
  - `first_semester_end`
  - `second_semester_start`
  - `second_semester_end`

## 4) Get subscription URL
- After deploy succeeds, your ICS URL is:
  - `https://<github-username>.github.io/<repo-name>/kyushu_timetable.ics`
- iPhone subscription usually accepts:
  - `webcal://<github-username>.github.io/<repo-name>/kyushu_timetable.ics`

## 5) Subscribe on iPhone
- `Settings` -> `Calendar` -> `Accounts` -> `Add Account` -> `Other` -> `Add Subscribed Calendar`
- Paste the `webcal://...` URL (or `https://...` if needed).
