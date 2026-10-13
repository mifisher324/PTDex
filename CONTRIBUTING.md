# Contributing

1. Fork the repository and create a branch for your change.
2. Keep changes focused; one feature or fix per pull request.
3. Run `python -m compileall -q .` and `python -m unittest test_page_cache` before opening the PR.
4. Open a pull request describing what changed and how you tested it (a local PEQ database is enough).

No server data belongs in this repository: no `configuration.ini`, database files,
passwords, tokens, host names or IP addresses, Discord ids, player or account data,
or forum content. Use `configuration.ini.example` and placeholders instead.
