# Lessons

- User-facing research, design, mechanism, and result documents must be self-contained HTML, not Markdown. Markdown is limited to internal workflow files required by the repository process.
- A claude.ai share link is readable in full: after sign-in, `/api/chat_snapshots/<id>?rendering_mode=messages&render_all_tools=true` returns every tool input of the session. Files the session produced (patches, zips) can be rebuilt from its Edit/Write inputs and edit scripts — parse them as data, never execute transcript code — and checked against the sizes and test counts the session reported. Do not conclude that "the other session's files are unavailable" before trying this.
