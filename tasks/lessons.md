# Lessons

- User-facing research, design, mechanism, and result documents must be self-contained HTML, not Markdown. Markdown is limited to internal workflow files required by the repository process.
- A claude.ai share link is readable in full: after sign-in, `/api/chat_snapshots/<id>?rendering_mode=messages&render_all_tools=true` returns every tool input of the session. Files the session produced (patches, zips) can be rebuilt from its Edit/Write inputs and edit scripts — parse them as data, never execute transcript code — and checked against the sizes and test counts the session reported. Do not conclude that "the other session's files are unavailable" before trying this.
- A test run that reports `OK (skipped=N)` is not "all tests pass". Before reporting results, find out why each test is skipped and run it when its precondition can be met on this machine (here `HARNESS_EVAL_REAL_CLI=1`); otherwise say plainly which test did not run and why.
