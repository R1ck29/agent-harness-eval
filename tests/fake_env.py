"""偽の CLI を使うテストのプロファイルに足す設定。

- sandbox: off … 偽の CLI は venv の Python とリポジトリの中のスクリプトで動くが、bwrap の中からは見えないため
  （隔離そのものは test_cheatproof.py の Sandbox で確かめる）
- inherit_env … 偽の CLI の振る舞いを切り替える FAKE_* を試行に渡す（環境変数は許可リストなので宣言が要る）
"""
FAKE_VARS = ["FAKE_ARGV_LOG", "FAKE_STATE_DIR", "FAKE_CODEX_JUDGE_VERDICT", "FAKE_IGNORE_DENY", "FAKE_IMPROVE", "FAKE_IMPROVE_MANIFEST",
             "FAKE_JUDGE_FAIL", "FAKE_JUDGE_PROMPT_LOG", "FAKE_JUDGE_USAGE", "FAKE_JUDGE_VERDICT", "FAKE_REFRESH_AUTH",
             "FAKE_JUDGE_OVERLAP_DIR", "FAKE_JUDGE_ACTUAL_MODEL", "FAKE_CODEX_JUDGE_ACTUAL_MODEL"]
SUFFIX = "\nsandbox: off\ninherit_env: [" + ", ".join(FAKE_VARS) + "]\n"
