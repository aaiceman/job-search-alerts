# Job Search Alerts

A weekday job-alert email for one person, built to run unattended on a Windows PC. It collects new postings from job sites and company career boards and filters them against your rules. Headless [Claude Code](https://claude.com/claude-code) then ranks them by fit, and the digest is emailed from your own Gmail to yourself.

Each job card shows a fit score with the reasons, pay compared to your current base, benefits compared to your current package, a summary of company reviews, and a note on any degree requirement. Dealbreakers (on-call rotations, work travel, clearance required, contract roles, staffing-agency reposts) are caught and listed separately so you can check the calls.

The method is written up in the playbook [Resume, LinkedIn & Job Search with Claude](https://aaicemans-ai-tools.gitbook.io/aaicemans-ai-tools-docs/playbooks/resume-linkedin-and-job-search-with-claude).

## How it works

```
Task Scheduler (Mon–Fri 05:45 + at-logon trigger)
  └─ scheduler/run.ps1 → python -m jobsearch.run
       1. Collect   JobSpy (LinkedIn, Indeed), Remotive, Himalayas, Jobicy, RemoteOK,
                    We Work Remotely, Greenhouse / Lever / Ashby company boards
       2. Dedupe    SQLite; anything already emailed is skipped
       3. Filter    hard drops on structured fields (title, location, employment type,
                    salary, excluded companies); free-text rules only add flags
       4. Rank      headless `claude -p`, locked down, writes ranked.json
       5. Score     Python computes the final score from the ranker's score parts
       6. Email     Gmail SMTP with an app password
```

A typical run takes 10–40 minutes, most of it collection and ranking. Ranking is capped at $8 per run (`--max-budget-usd`).

## Requirements

- Windows 10 or 11 (Task Scheduler, PowerShell 5.1 for `run.ps1`)
- Python 3.12 or newer (developed on 3.14)
- Claude Code CLI installed with npm and signed in: `claude auth login`, check with `claude auth status`. The standalone CLI has its own sign-in, separate from the desktop app.
- A Gmail account with 2-Step Verification and an [app password](https://myaccount.google.com/apppasswords)

## Setup

The scripts assume this layout. Change the paths in `scheduler/*.ps1`, `scheduler/ranker-settings.json` and `JOBSEARCH_DATA` if you use another.

```
C:\JobSearch\app    this repo
C:\JobSearch\data   private: secrets, profile, database, logs (never commit it, keep it out of OneDrive)
```

1. **Clone and install**

   ```powershell
   git clone https://github.com/aaiceman/job-search-alerts C:\JobSearch\app
   cd C:\JobSearch\app
   python -m venv .venv
   .venv\Scripts\python -m pip install -r requirements.txt
   .venv\Scripts\python -m pytest -q
   ```

2. **Create the private data folder** from the examples:

   | Copy | To | What it holds |
   | --- | --- | --- |
   | `.env.example` | `data\.env` | Gmail address and app password |
   | `config\ranker-profile.example.md` | `data\profile\ranker-profile.md` | Your skills, target roles, education, current benefits, priorities. No name, employer or salary. |
   | `config\owner.example.yaml` | `data\config\owner.yaml` | Current base salary and the minimum raise you want (sets the pay target) |
   | `config\exclusions.example.yaml` | `data\config\exclusions.yaml` | Companies to never show (your employer, its owners, staffing agencies) |
   | `config\watch.example.yaml` | `data\config\watch.yaml` | Optional: postings you applied to; the digest shows when they close |

   Lock the folder to your account only. For example: `icacls C:\JobSearch\data /inheritance:r /grant:r "${env:USERNAME}:(OI)(CI)F"`.

3. **Tune the shared config** in `config\`:
   - `filters.yaml`: salary floor, home state, title include/exclude patterns, ranker model and timeout.
   - `searches.yaml`: search terms per source, look-back window.
   - `portals.yml`: company career boards to watch (regenerate with `scripts\discover_portals.py`).
   - `prompts\rank.md`: the scoring rubric and dealbreakers. The role taxonomy and skill weighting assume an IT / security / systems candidate; adjust them for other fields.

4. **Run the lock-down probe** in [`docs/lockdown-probe.md`](docs/lockdown-probe.md). It confirms the ranker can't run shell commands, read your secrets, write outside its run folder, or stop for a permission prompt.

5. **Try it without sending:**

   ```powershell
   .venv\Scripts\python -m jobsearch.collect --dry-run --window-hours 72
   .venv\Scripts\python -m jobsearch.run --no-send --force
   ```

   The preview goes to `data\runs\<date>-preview\` (`digest.preview.html`). The real run folder is never touched.

6. **Schedule it** from an elevated PowerShell:

   ```powershell
   .\scheduler\register-task.ps1 -LogonType Interactive
   ```

   `Interactive` runs only while you are signed in. If the PC rebooted overnight, the at-logon trigger sends the digest at your next sign-in, marked late. `S4U` runs without a sign-in, but check that `claude` still authenticates that way before you rely on it (see `CLAUDE.md`).

## Operations

- Exit codes: `0` ok or already sent, `2` send failed (a rerun resends the saved digest), `3` crashed (a crash email is sent), `4` another run holds the lock.
- Logs: `data\logs\<date>.log` and `data\logs\task-stdout.log`. Run artifacts: `data\runs\<date>\`.
- After a Claude Code upgrade, run the lock-down probe again.
- `CLAUDE.md` is the detailed maintainer guide: time budgets, the circuit breaker, filter rules and gotchas.

## Privacy

- Everything personal stays in the data folder, which `.gitignore` excludes. The repo only has example files.
- The ranker gets a profile with no name, employer or salary. The "% vs current base" text is computed locally in Python.
- Before you push a fork anywhere, scan the full git history for your name, email and employer.

## Scraping and terms of use

Collection uses [JobSpy](https://github.com/speedyapply/JobSpy), which reads public LinkedIn and Indeed search pages without signing in. Those sites' terms of service prohibit automated access. Expect throttling (HTTP 429), which the collector handles with a page-fetch cap, a circuit breaker and one retry. Keep the request volume low, never point the collector at a signed-in session, and don't add proxy rotation or CAPTCHA workarounds. Any other use is your responsibility. The other sources are public APIs or feeds built for this kind of use.

## License

MIT. See [LICENSE](LICENSE) and [NOTICE.md](NOTICE.md) for third-party credits.
