"""Phase 5: the dashboard starts, renders every screen and survives the replay controls."""
import warnings

import pytest

warnings.filterwarnings("ignore", category=DeprecationWarning)


@pytest.fixture()
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("FALCON_DB_DIR", str(tmp_path))
    monkeypatch.setenv("FALCON_LLM", "off")
    import streamlit as st
    from streamlit.testing.v1 import AppTest
    st.cache_resource.clear()
    at = AppTest.from_file("../app.py", default_timeout=120).run()
    yield at
    st.cache_resource.clear()


def labels(at):
    return [b.label for b in at.sidebar.button]


def test_dashboard_renders_and_replay_controls_work(app):
    assert not app.exception
    assert app.sidebar.metric[0].value == "13:45"
    nxt = next(b for b in app.sidebar.button if b.label.startswith("▶ Next event"))
    assert "S1 at 14:05" in nxt.label
    nxt.click().run()
    assert not app.exception and app.sidebar.metric[0].value == "14:05"
    next(b for b in app.sidebar.button if b.label.startswith("▶ Next event")).click().run()     # S2 at 14:10
    assert not app.exception and app.sidebar.metric[0].value == "14:10"
    assert any("approval request(s) waiting" in w.value for w in app.warning)                   # the swap needs Duty Ops
    approve = next(b for b in app.button if b.label == "✅ Approve")
    approve.click().run()
    assert not app.exception
    assert not any("approval request(s) waiting" in w.value for w in app.warning)
    next(b for b in app.sidebar.button if b.label == "+15 min").click().run()
    assert not app.exception and app.sidebar.metric[0].value == "14:25"
