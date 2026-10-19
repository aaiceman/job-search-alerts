# Third-party notices

- **python-jobspy** (speedyapply/JobSpy, MIT): installed from PyPI as a dependency, not vendored. This project calls one private JobSpy method (`LinkedIn._fetch_details`) for the two-phase LinkedIn fetch; tests guard it so a JobSpy upgrade that renames it fails loudly.
- **career-ops** (career-ops-hq/career-ops, MIT): some company names in `config/portals.yml` and `scripts/extra-names.txt` were taken from its `templates/portals.example.yml`. The Greenhouse/Lever/Ashby portal-scanning approach was also inspired by it. No career-ops code is included.
