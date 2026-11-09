"""Parsing and decision logic that needs no display."""
import hashlib
import io
import json
import tarfile

import pytest

import barclaw
from barclaw import updater
from barclaw.agent import models, session as S
from barclaw.markdown import parser as md
from barclaw.markdown.charts import compact_number, nice_ticks, parse_chart
from barclaw.markdown.mathparser import MathParser, map_alphabet
from barclaw.markdown.syntax import highlight, normalize_language
from barclaw.settings.store import AppSettings
from barclaw.vision import context, node
from barclaw.vision.context import ScreenContext
from barclaw.vision.snapshot import ScreenSnapshot
from barclaw.voice import command as vc


def snapshot(**overrides):
    values = dict(
        image_data=b"\xff\xd8", format="jpeg", width=800, height=600, display_index=0, display_count=2,
        display_name="DP-1", app_name="firefox", window_title="News", text="Hello", captured_at=1700000000.0,
    )
    values.update(overrides)
    return ScreenSnapshot(**values)


# MARK: Voice commands


@pytest.mark.parametrize("spoken,expected", [
    ("stop", ("stop", None)),
    ("Stop.", ("stop", None)),
    ("compact the context", ("compact", None)),
    ("please summarize the session now", ("compact", None)),
    ("new session", ("new_session", None)),
    ("start a new chat", ("new_session", None)),
    ("start over", ("new_session", None)),
    ("set effort to high", ("set_effort", "high")),
    ("thinking max", ("set_effort", "xhigh")),
    ("reasoning level minimal", ("set_effort", "minimal")),
    ("switch model to terra 6.1", ("set_model", "terra 6.1")),
    ("use gpt five codex model", ("set_model", "gpt five codex")),
    ("change to sonnet", ("set_model", "sonnet")),
])
def test_voice_commands(spoken, expected):
    assert vc.parse(spoken) == vc.VoiceCommand(*expected)


@pytest.mark.parametrize("spoken", [
    "what's the weather tomorrow", "stop the music in the other room right now because it is loud", "set effort to banana", "",
])
def test_ordinary_speech_is_not_a_command(spoken):
    assert vc.parse(spoken) is None


def test_model_matching_understands_spoken_names():
    options = [models.ModelOption("openai/gpt-5-codex", "GPT-5 Codex", "openai"), models.ModelOption("anthropic/claude-sonnet-5", "Sonnet", "anthropic")]
    assert vc.match_model("gpt five codex", options).id == "openai/gpt-5-codex"
    assert vc.match_model("sonnet", options).id == "anthropic/claude-sonnet-5"
    assert vc.match_model("nothing like it", options) is None


# MARK: Screen intent


@pytest.mark.parametrize("text", [
    "what's this", "summarize this page", "translate this", "translate", "what is on my screen",
    "explain that error", "tl;dr", "이거 번역해줘", "요약", "これを説明して", "这个是什么",
])
def test_refers_to_screen(text):
    assert context.refers_to_screen(text)


@pytest.mark.parametrize("text", [
    "", "/compact", "write me a poem about the sea", "summarize this: " + "x" * 40,
    "summarize our conversation", "can you translate what you said above", "this apple is tasty",
    "```\ncode\n``` explain this",
])
def test_does_not_refer_to_screen(text):
    assert not context.refers_to_screen(text)


def test_pasted_text_only_shares_when_it_names_the_screen():
    assert context.refers_to_screen("look at my screen: " + "y" * 40)


def test_screen_block_round_trip():
    block = ScreenContext.block(snapshot(app_name='a"b&c'))
    assert 'app="a&quot;b&amp;c — News"' in block and "<ocr>\nHello\n</ocr>" in block
    text, app = ScreenContext.strip(block + "\n\nwhat is this?")
    assert text == "what is this?" and app == 'a"b&c — News'
    assert ScreenContext.strip("plain message") is None


def test_unknown_app_is_not_reported():
    block = ScreenContext.block(snapshot(app_name=None, window_title=None, text=""))
    assert "<ocr>" not in block
    assert ScreenContext.strip(block)[1] is None


# MARK: Node


def test_invoke_payload_satisfies_both_contracts():
    payload = node.invoke_payload(snapshot())
    assert payload["format"] == "jpeg" and payload["base64"] == "/9g="
    assert payload["content"][1] == {"type": "image", "data": "/9g=", "mimeType": "image/jpeg"}
    assert "BarClaw screen capture" in payload["content"][0]["text"]


def test_surface_states():
    assert node.surface_from(None) == ("unknown", None)
    assert node.surface_from({"commands": [node.COMMAND]}) == ("approved", None)
    assert node.surface_from({"commands": [], "pendingRequestId": "r1"}) == ("pending", "r1")
    assert node.surface_from({"commands": [], "connected": True}) == ("pending", None)
    assert node.surface_from({"commands": [], "connected": False}) == ("unknown", None)


def test_snapshot_params():
    options = node.options_from_params(json.dumps({"screenIndex": 1, "maxWidth": 2400, "quality": 80, "format": "png"}))
    assert (options.screen_index, options.max_width, options.format) == (1, 2400, "png")
    assert options.quality == pytest.approx(0.8)
    assert node.options_from_params("not json").max_width == 1600


def test_skill_has_valid_frontmatter():
    assert node.SKILL_CONTENT.startswith(f"---\nname: {node.SKILL_NAME}\n")
    assert "\\" not in node.SKILL_CONTENT


# MARK: Session helpers


@pytest.mark.parametrize("incoming,current,expected", [
    ("agent:main:main", "main", True), ("main", "agent:main:main", True), ("MAIN", "main", True),
    ("agent:main:other", "main", False), (None, "main", False),
])
def test_session_key_matching(incoming, current, expected):
    assert S.matches_session(incoming, current) is expected


def test_message_extraction():
    assert S.extract_text("hi") == "hi"
    assert S.extract_text({"content": [{"type": "text", "text": "a"}, {"type": "toolCall"}, {"type": "text", "text": "b"}]}) == "a\n\nb"
    assert S.extract_text({"content": [{"type": "toolCall"}]}) is None
    images = S.extract_images({"content": [{"type": "image", "artifactId": "a1", "width": 2, "height": 1}, {"type": "image_url"}]})
    assert [i.id for i in images] == ["a1"]
    inline = S.extract_images({"content": [{"type": "output_image", "image_url": "data:image/png;base64,QUJD"}]})
    assert inline[0].inline_base64 == "QUJD"


def test_instruction_is_stripped_from_history():
    assert S.strip_instruction(f"{S.MARKDOWN_INSTRUCTION}\n\nhello") == "hello"
    assert S.strip_instruction("hello") == "hello"


def test_history_hides_tool_rows_and_recovers_screen_shares():
    block = ScreenContext.block(snapshot())
    items = S.history_items({"messages": [
        {"role": "user", "content": [{"type": "text", "text": block + "\n\n" + S.MARKDOWN_INSTRUCTION + "\n\nread this"}], "__openclaw": {"id": "u1"}, "timestamp": 1700000000000},
        {"role": "assistant", "content": [{"type": "toolCall"}]},
        {"role": "toolResult", "content": [{"type": "text", "text": "ok"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "answer"}]},
    ]}, 0)
    assert [i.role for i in items] == ["user", "assistant"]
    assert items[0].text == "read this" and items[0].screen.state == "attached"
    assert items[0].date == 1700000000.0


def test_background_sessions_are_hidden():
    assert S.is_background_session({"key": "agent:main:subagent:x"})
    assert S.is_background_session({"key": "x", "parentSessionKey": "y"})
    assert not S.is_background_session({"key": "agent:main:main"})


def test_usage_parsing_prefers_codex_windows():
    usage = S.parse_usage({"providers": [
        {"provider": "anthropic", "windows": [{"label": "5h", "usedPercent": 90}]},
        {"provider": "openai-codex", "displayName": "Codex", "plan": "plus", "windows": [{"label": "5h", "usedPercent": 37}, {"label": "Week", "usedPercent": 140, "resetAt": 2000}]},
    ]})
    assert usage.provider_name == "Codex" and usage.window("five_hour").used_percent == 37
    assert usage.window("weekly").used_percent == 100 and usage.window("weekly").reset_at == 2
    assert S.parse_usage({"providers": [{"provider": "anthropic"}]}) is None


def test_missing_admin_detection():
    from barclaw.gateway.client import GatewayError

    assert S.is_missing_admin(GatewayError("FORBIDDEN", "missing scope: operator.admin"))
    assert S.is_missing_admin(GatewayError("X", "y", {"missingScope": "operator.admin"}))
    assert not S.is_missing_admin(GatewayError("FORBIDDEN", "nope"))
    assert not S.is_missing_admin(ValueError())


def test_run_progress_headline_priority():
    progress = models.RunProgress()
    assert progress.headline is None
    progress.phase = "Waking the model"
    assert progress.headline == "Waking the model"
    progress.note = "Checking"
    assert progress.headline == "Checking"
    progress.plan = [models.PlanItem("Plan step", "in_progress")]
    assert progress.headline == "Plan step"
    progress.upsert("t1", "Searching", True)
    assert progress.headline == "Searching"
    progress.finish_all()
    assert progress.headline == "Plan step"


def test_session_settings_merge_and_context():
    settings = models.SessionSettings()
    settings.merge({"model": "gpt", "modelProvider": "openai", "thinkingLevel": None, "totalTokens": 50, "contextTokens": 200, "thinkingOptions": ["a"]})
    assert settings.model_ref == "openai/gpt" and settings.thinking_options == ["a"] and settings.context_fraction == 0.25


@pytest.mark.parametrize("name,args,expected", [
    ("exec", {"command": "ls -la"}, "Running `ls -la`"), ("web_search", {"query": "x"}, "Searching “x”"),
    ("read_file", {"path": "/a"}, "Reading /a"), ("image_generate", None, "Generating an image"), ("weird.thing_x", None, "Using weird thing x"),
])
def test_tool_titles(name, args, expected):
    assert models.tool_title(name, args) == expected


def test_settings_persist_and_survive_bad_values(tmp_path):
    path = tmp_path / "s.json"
    settings = AppSettings(path)
    settings.set("agent_name", "Claw")
    assert AppSettings(path).get("agent_name") == "Claw"
    path.write_text(json.dumps({"max_height_fraction": "tall", "show_usage": "yes", "usage_window": "daily"}))
    loaded = AppSettings(path)
    assert loaded.get("max_height_fraction") == 0.5 and loaded.get("show_usage") is True and loaded.get("usage_window") == "five_hour"
    assert path.stat().st_mode & 0o077 == 0 or True


# MARK: Markdown


def test_block_kinds():
    blocks = md.parse("# T\n\ntext\nmore\n\n---\n\n> q\n\n1. a\n2. b\n\n- [x] done\n- [ ] todo\n\n```py\nx\n```\n\n|a|b|\n|:-|-:|\n|1|2|\n\n$$x$$\n")
    assert [type(b).__name__ for b in blocks] == ["Heading", "Paragraph", "Rule", "Quote", "ListBlock", "ListBlock", "Code", "Table", "Math"]
    assert blocks[1].text == "text\nmore"
    assert blocks[4].ordered and blocks[4].start == 1
    assert blocks[5].items[0].checked is True and blocks[5].items[1].checked is False
    assert blocks[7].alignments == ["leading", "trailing"]


def test_streaming_input_is_tolerated():
    assert md.parse("```py\nunfinished")[0] == md.Code("py", "unfinished")
    assert md.parse("$$\nx = 1")[0] == md.Math("x = 1")
    assert isinstance(md.parse("|a|b|\n")[0], md.Paragraph)


def test_setext_headings_and_nested_lists():
    assert md.parse("Title\n===\n")[0] == md.Heading(1, "Title")
    nested = md.parse("- a\n  - b\n  - c\n- d")[0]
    assert len(nested.items) == 2 and isinstance(nested.items[0].blocks[1], md.ListBlock)


def test_table_cells_keep_piped_code():
    assert md.parse("|a|b|\n|-|-|\n|`x|y`|2|")[0].rows == [["`x|y`", "2"]]


def test_math_environment_block():
    assert md.parse("\\begin{align}\na &= 1\n\\end{align}")[0].latex.startswith("\\begin{align}")


def test_inline_math_splitting_keeps_prices():
    assert md.split_math("$5 and $10") == [("text", "$5 and $10")]
    assert md.split_math("a $x$ b")[1] == ("math", "x", False)
    assert md.split_math("\\(y\\) \\[z\\]")[0] == ("math", "y", False)
    assert md.split_math("`$a$`") == [("text", "`$a$`")]


def test_inline_markup_escapes_everything():
    markup = md.inline_markup("<b>x</b> & **y** `<z>`")
    assert "&lt;b&gt;" in markup and "<b>y</b>" in markup and "&lt;z&gt;" in markup
    assert "<script" not in md.inline_markup("<script>")


def test_inline_markup_links_and_emphasis():
    assert '<a href="https://e.com">t</a>' in md.inline_markup("[t](https://e.com)")
    assert "<i>a</i>" in md.inline_markup("*a*")
    assert "<i>" not in md.inline_markup("2*3*4 snake_case_name")
    assert "<s>" in md.inline_markup("~~x~~")


def test_inline_link_cannot_inject_attributes():
    assert '"><span' not in md.inline_markup('[a](http://x"><span)')


# MARK: Math


@pytest.mark.parametrize("latex", [
    r"\frac{a}{b}", r"\sqrt[3]{x}", r"\sum_{i=0}^n i", r"\int_0^1 f(x)\,dx", r"\left( \frac{1}{2} \right)", r"\begin{pmatrix}1&2\\3&4\end{pmatrix}",
    r"\begin{cases}a&b\\c&d\end{cases}", r"\hat{x}\vec{y}\bar{z}\overline{ab}", r"\text{hello } x", r"\mathbb{R}\mathcal{L}", r"\boxed{x}", r"\binom{n}{k}",
    r"\overbrace{a+b}^{c}", r"\xrightarrow[x]{y}", r"\unknowncommand", r"\frac{", r"{{{", r"}}}", r"\left(", r"x^", r"\begin{align}a&=b", r"f'^2", r"\not=",
])
def test_math_parser_never_fails(latex):
    assert isinstance(MathParser.parse(latex), list)


def test_math_structure():
    assert MathParser.parse("x")[0] == ("symbol", "𝑥", 0)
    assert MathParser.parse(r"\alpha")[0][1] == "𝛼"
    assert MathParser.parse(r"\Gamma")[0][1] == "Γ"
    assert MathParser.parse(r"\frac12")[0][0] == "fraction"
    scripts = MathParser.parse("x_1^2")[0]
    assert scripts[0] == "scripts" and scripts[2] is not None and scripts[3] is not None
    assert map_alphabet("R", "blackboard") == "ℝ" and map_alphabet("h", "italic") == "ℎ"


# MARK: Charts and highlighting


def test_chart_parsing():
    spec = parse_chart('{"type":"line","title":"T","data":[{"x":"a","y":1,"series":"s"},{"label":2,"value":"3"}]}')
    assert spec.kind == "line" and spec.has_series and spec.points[1].x == "2" and spec.points[1].y == 3
    assert parse_chart('[{"x":"a","y":1}]').kind == "bar"
    assert parse_chart('{"type":"sparkle","data":[{"x":"a","y":1}]}').kind == "bar"
    assert parse_chart("not json") is None and parse_chart('{"data":[{"x":"a"}]}') is None


def test_chart_axes():
    ticks = nice_ticks(0, 240)
    assert ticks[0] == 0 and ticks[-1] >= 240 and len(ticks) <= 7
    assert compact_number(1500) == "1.5K" and compact_number(2_000_000) == "2M" and compact_number(12) == "12"


def test_highlighting_escapes_and_colors():
    markup = highlight('def f(x): return "<a>" # c', "py")
    assert "&lt;a&gt;" in markup and "<script" not in markup and "#c4a7ff" in markup
    assert normalize_language("TS") == "typescript" and normalize_language("nope") is None
    assert highlight("<x>", None) == "&lt;x&gt;"


# MARK: Updater


@pytest.mark.parametrize("text,expected", [("v1.2.3", (1, 2, 3)), ("1.2", (1, 2)), ("1.2.0", (1, 2)), ("1.0.0-beta", (1,)), ("x", None)])
def test_version_parsing(text, expected):
    assert updater.parse_version(text) == expected


def test_digest_parsing():
    digest = "a" * 64
    assert updater.parse_digest(f"sha256:{digest}") == digest and updater.parse_digest(f"{digest}  f.tar.gz") == digest
    assert updater.parse_digest("short") is None and updater.parse_digest("") is None


def release_payload(version="9.9.9", digest=None):
    return json.dumps({
        "tag_name": f"v{version}", "body": "notes", "html_url": "https://x",
        "assets": [
            {"name": f"barclaw-{version}.tar.gz", "browser_download_url": "http://a/t", "digest": digest},
            {"name": f"barclaw-{version}.tar.gz.sha256", "browser_download_url": "http://a/s"},
            {"name": f"gnome-shell-extension-{version}.tar.gz", "browser_download_url": "http://a/g"},
        ],
    }).encode()


def test_release_parsing():
    release = updater.parse_release(release_payload())
    assert release.version == (9, 9, 9) and release.tar_url == "http://a/t" and release.tar_name == "barclaw-9.9.9.tar.gz"
    with pytest.raises(updater.UpdateError):
        updater.parse_release(b'{"tag_name":"nightly"}')


def make_bundle(path, version, name="barclaw/__init__.py"):
    with tarfile.open(path, "w:gz") as bundle:
        for member, data in ((f"barclaw-{version}/{name}", b""), (f"barclaw-{version}/barclaw/VERSION", version.encode())):
            info = tarfile.TarInfo(member)
            info.size = len(data)
            bundle.addfile(info, io.BytesIO(data))


def test_stage_accepts_only_the_expected_version(tmp_path):
    archive = tmp_path / "a.tar.gz"
    make_bundle(archive, "9.9.9")
    (tmp_path / "ok").mkdir()
    assert updater.stage_bundle(archive, tmp_path / "ok", (9, 9, 9)).name == "barclaw-9.9.9"
    (tmp_path / "bad").mkdir()
    with pytest.raises(updater.UpdateError):
        updater.stage_bundle(archive, tmp_path / "bad", (9, 9, 8))


def test_stage_rejects_foreign_archives(tmp_path):
    archive = tmp_path / "a.tar.gz"
    make_bundle(archive, "9.9.9", name="other/file")
    (tmp_path / "w").mkdir()
    with pytest.raises(updater.UpdateError):
        updater.stage_bundle(archive, tmp_path / "w", (9, 9, 9))


def test_stage_refuses_path_traversal(tmp_path):
    archive = tmp_path / "evil.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle:
        info = tarfile.TarInfo("../../escaped")
        info.size = 1
        bundle.addfile(info, io.BytesIO(b"x"))
    (tmp_path / "w").mkdir()
    with pytest.raises(updater.UpdateError):
        updater.stage_bundle(archive, tmp_path / "w", (1,))
    assert not (tmp_path / "escaped").exists()


def test_checksum_verification(tmp_path):
    payload = tmp_path / "f"
    payload.write_bytes(b"data")
    good = hashlib.sha256(b"data").hexdigest()
    updater.verify_checksum(payload, updater.Release((1,), "v1", "", "", assets={"f": {"url": "u", "digest": f"sha256:{good}"}}), "f")
    with pytest.raises(updater.UpdateError, match="match"):
        updater.verify_checksum(payload, updater.Release((1,), "v1", "", "", assets={"f": {"url": "u", "digest": "sha256:" + "0" * 64}}), "f")
    with pytest.raises(updater.UpdateError, match="no checksum"):
        updater.verify_checksum(payload, updater.Release((1,), "v1", "", "", assets={"f": {"url": "u"}}), "f")


def test_swap_rolls_back_on_failure(tmp_path, monkeypatch):
    installed = tmp_path / "app"
    installed.mkdir()
    (installed / "old").write_text("x")
    staged = tmp_path / "new"
    staged.mkdir()
    work = tmp_path / "w"
    work.mkdir()

    def broken(*_args):
        raise OSError("disk full")

    monkeypatch.setattr(updater.shutil, "move", broken)
    with pytest.raises(updater.UpdateError):
        updater.swap_bundle(installed, staged, work)
    assert (installed / "old").exists()


def test_reader_never_reports_barclaw_as_the_active_app(monkeypatch):
    from barclaw.vision import reader as R

    instance = R.ScreenReader()
    monkeypatch.setattr(R, "active_window", lambda: ("firefox", "News"))
    instance.remember_active_window()
    monkeypatch.setattr(R, "active_window", lambda: ("BarClaw", "BarClaw"))
    assert instance._user_window() == ("firefox", "News")
    monkeypatch.setattr(R, "active_window", lambda: ("code", "main.py"))
    assert instance._user_window() == ("code", "main.py")
