"""User accounts and the session gate.

The threat model is modest and worth stating so the tests are not mistaken for
more than they are: this is a loopback-bound tool, and the login is here for
multi-user demos and because the brief asks for it, not to withstand a network
attacker. So these check the properties that would embarrass the feature if
they broke — a password stored in the clear, a session that outlives logout, a
wrong password that gets in, an /api route reachable without signing in — and
not cryptographic hardness the deployment does not call for.
"""

from __future__ import annotations

import pytest

from companies_research.webapp import auth


@pytest.fixture(autouse=True)
def _fresh(monkeypatch, tmp_path):
    # conftest already points DB_PATH at tmp; reload so db_path.parent is tmp
    # here too, and every test starts with no accounts.
    from companies_research.config import reload_settings

    reload_settings()
    yield


@pytest.fixture
def open_signup(monkeypatch):
    """Some tests genuinely need a second account; the default forbids it."""
    monkeypatch.setenv("SIGNUP_OPEN", "true")
    from companies_research.config import reload_settings

    reload_settings()
    yield


# --- account creation --------------------------------------------------------


def test_a_new_account_can_log_in():
    auth.create_user(email="chris@example.com", password="demo-pass-8", name="Chris")
    user = auth.authenticate(email="chris@example.com", password="demo-pass-8")
    assert user.email == "chris@example.com"
    assert user.name == "Chris"


def test_email_is_case_and_space_insensitive():
    auth.create_user(email="Chris@Example.com", password="demo-pass-8")
    assert auth.authenticate(email="  chris@example.com ", password="demo-pass-8")


def test_a_wrong_password_is_refused():
    auth.create_user(email="a@b.com", password="correct-horse")
    with pytest.raises(auth.AuthError):
        auth.authenticate(email="a@b.com", password="wrong")


def test_an_unknown_email_is_refused_the_same_way():
    """The message must not distinguish 'no such user' from 'wrong password'."""
    auth.create_user(email="a@b.com", password="correct-horse")
    try:
        auth.authenticate(email="nobody@b.com", password="whatever")
    except auth.AuthError as exc:
        assert "incorrect" in str(exc).lower()
    else:
        pytest.fail("an unknown account logged in")


def test_a_duplicate_email_is_rejected(open_signup):
    auth.create_user(email="a@b.com", password="first-pass-8")
    with pytest.raises(auth.AuthError, match="already exists"):
        auth.create_user(email="a@b.com", password="second-pass-8")


@pytest.mark.parametrize("bad", ["", "short", "1234567"])
def test_a_weak_password_is_rejected(bad):
    with pytest.raises(auth.AuthError):
        auth.create_user(email="a@b.com", password=bad)


@pytest.mark.parametrize("bad", ["", "not-an-email", "a@b", "@b.com"])
def test_an_invalid_email_is_rejected(bad):
    with pytest.raises(auth.AuthError):
        auth.create_user(email=bad, password="demo-pass-8")


# --- storage -----------------------------------------------------------------


def test_the_password_is_never_stored_in_the_clear():
    """The one property a password store cannot get wrong."""
    import sqlite3

    from companies_research.config import SETTINGS

    auth.create_user(email="a@b.com", password="super-secret-1")
    db = SETTINGS.db_path.parent / "accounts.db"
    conn = sqlite3.connect(db)
    row = conn.execute("SELECT pw_hash, pw_salt FROM users").fetchone()
    blob = " ".join(str(c) for c in row)
    assert "super-secret-1" not in blob
    # and the whole file, in case it lands anywhere else
    assert b"super-secret-1" not in db.read_bytes()


def test_two_users_with_the_same_password_get_different_hashes(open_signup):
    """A per-user salt is what stops one crack from cracking them all."""
    import sqlite3

    from companies_research.config import SETTINGS

    auth.create_user(email="a@b.com", password="identical-pass")
    auth.create_user(email="c@d.com", password="identical-pass")
    conn = sqlite3.connect(SETTINGS.db_path.parent / "accounts.db")
    hashes = [r[0] for r in conn.execute("SELECT pw_hash FROM users")]
    assert hashes[0] != hashes[1]


# --- sessions ----------------------------------------------------------------


def test_a_session_resolves_to_its_user():
    user = auth.create_user(email="a@b.com", password="demo-pass-8")
    token = auth.open_session(user)
    assert auth.user_for_session(token).email == "a@b.com"


def test_logout_invalidates_the_session():
    user = auth.create_user(email="a@b.com", password="demo-pass-8")
    token = auth.open_session(user)
    auth.close_session(token)
    assert auth.user_for_session(token) is None


def test_an_expired_session_is_rejected(monkeypatch):
    user = auth.create_user(email="a@b.com", password="demo-pass-8")
    monkeypatch.setattr(auth, "_SESSION_TTL", -1)   # already expired
    token = auth.open_session(user)
    assert auth.user_for_session(token) is None


def test_a_forged_token_resolves_to_nobody():
    assert auth.user_for_session("not-a-real-token") is None
    assert auth.user_for_session("") is None


# --- the gate, end to end ----------------------------------------------------


def _client(monkeypatch, tmp_path):
    from starlette.testclient import TestClient

    from companies_research.config import reload_settings
    reload_settings()
    from companies_research.webapp import server

    # base_url sets the Host header to an allowed loopback host — the guard
    # middleware rejects an unrecognised Host (a DNS-rebinding defence), and
    # TestClient otherwise sends "testserver".
    client = TestClient(server.app, base_url="http://127.0.0.1:8765", client=("127.0.0.1", 50000))
    # every /api call carries the run token; the session cookie is what these
    # tests are actually exercising.
    client.headers.update({"x-cr-token": server.TOKEN,
                           "origin": "http://127.0.0.1:8765"})
    return client


def test_a_protected_route_is_401_without_a_session(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    assert client.get("/api/state").status_code == 401


def test_signup_then_the_same_client_reaches_a_protected_route(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    signup = client.post("/api/auth/signup",
                         json={"email": "a@b.com", "password": "demo-pass-8"})
    assert signup.status_code == 200
    # the cookie jar now carries the session
    assert client.get("/api/state").status_code == 200


def test_logout_closes_the_gate_again(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    client.post("/api/auth/signup", json={"email": "a@b.com", "password": "demo-pass-8"})
    assert client.get("/api/state").status_code == 200
    client.post("/api/auth/logout")
    assert client.get("/api/state").status_code == 401


def test_the_auth_routes_are_reachable_without_a_session(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    assert client.get("/api/auth/status").status_code == 200


def test_status_reports_whether_any_account_exists(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    assert client.get("/api/auth/status").json()["has_users"] is False
    client.post("/api/auth/signup", json={"email": "a@b.com", "password": "demo-pass-8"})
    assert client.get("/api/auth/status").json()["has_users"] is True


# --- first-user lockdown -----------------------------------------------------


def test_the_first_account_claims_the_instance():
    """The default: one signup succeeds, the next is refused."""
    auth.create_user(email="owner@example.com", password="demo-pass-8")
    with pytest.raises(auth.AuthError) as exc:
        auth.create_user(email="intruder@example.com", password="demo-pass-8")
    # the message must help both readers: the visitor and the operator
    assert "claimed" in str(exc.value).lower()
    assert "SIGNUP_OPEN" in str(exc.value)


def test_signup_open_reopens_registration(open_signup):
    auth.create_user(email="a@example.com", password="demo-pass-8")
    auth.create_user(email="b@example.com", password="demo-pass-8")
    assert auth.user_count() == 2


def test_login_still_works_after_the_instance_is_claimed():
    """Lockdown blocks new accounts, never existing ones."""
    auth.create_user(email="owner@example.com", password="demo-pass-8")
    assert auth.authenticate(email="owner@example.com", password="demo-pass-8")


def test_the_signup_endpoint_refuses_a_second_account(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    assert client.post("/api/auth/signup",
                       json={"email": "a@b.com", "password": "demo-pass-8"}).status_code == 200
    second = client.post("/api/auth/signup",
                         json={"email": "c@d.com", "password": "demo-pass-8"})
    assert second.status_code == 400
    assert "claimed" in second.json()["detail"].lower()


# --- Google sign-in ----------------------------------------------------------
# The design in full: identity is the Gmail address from users.getProfile (no
# OAuth scope change); the flow is loopback-only so it is refused on any
# non-local host; a Google account has no password and can never be
# password-logged-in; and creating one respects the same first-user lockdown as
# email signup. None of these tests invoke the real OAuth flow — the endpoint
# tests inject a fast fake job so no browser is launched.


def test_google_signin_creates_a_passwordless_account():
    import sqlite3

    from companies_research.config import SETTINGS

    user = auth.login_or_create_google(email="chris@gmail.com", name="Chris")
    assert user.email == "chris@gmail.com"
    row = sqlite3.connect(SETTINGS.db_path.parent / "accounts.db").execute(
        "SELECT auth_provider, pw_hash, rounds FROM users WHERE email = ?",
        ("chris@gmail.com",)).fetchone()
    assert row == ("google", "", 0)


def test_a_second_google_signin_logs_in_not_duplicates():
    first = auth.login_or_create_google(email="chris@gmail.com")
    second = auth.login_or_create_google(email="chris@gmail.com")
    assert first.id == second.id
    assert auth.user_count() == 1


def test_a_google_account_cannot_be_password_logged_in():
    """It has no password; the attempt must fail like any wrong password —
    and must not raise ValueError from pbkdf2 seeing rounds=0."""
    auth.login_or_create_google(email="chris@gmail.com")
    with pytest.raises(auth.AuthError, match="incorrect"):
        auth.authenticate(email="chris@gmail.com", password="anything-8x")


def test_google_signin_links_to_an_existing_local_account_without_touching_the_password():
    import sqlite3

    from companies_research.config import SETTINGS

    auth.create_user(email="chris@gmail.com", password="local-pass-8", name="Chris")
    db = SETTINGS.db_path.parent / "accounts.db"
    before = sqlite3.connect(db).execute(
        "SELECT pw_hash FROM users WHERE email = ?", ("chris@gmail.com",)).fetchone()[0]

    linked = auth.login_or_create_google(email="Chris@Gmail.com")   # same address, verified
    # logs into the SAME account
    assert auth.authenticate(email="chris@gmail.com", password="local-pass-8").id == linked.id
    # and the password row is untouched
    after = sqlite3.connect(db).execute(
        "SELECT pw_hash FROM users WHERE email = ?", ("chris@gmail.com",)).fetchone()[0]
    assert before == after and before != ""


def test_google_account_creation_respects_the_lockdown():
    auth.create_user(email="owner@example.com", password="owner-pass-8")   # claims the box
    with pytest.raises(auth.AuthError, match="claimed"):
        auth.login_or_create_google(email="stranger@gmail.com")


def test_google_signin_logs_into_an_existing_account_even_under_lockdown():
    """Lockdown blocks new accounts, never a returning one."""
    owner = auth.login_or_create_google(email="owner@gmail.com")   # first user, allowed
    again = auth.login_or_create_google(email="owner@gmail.com")   # returning, still allowed
    assert owner.id == again.id


def test_a_bad_email_from_the_provider_is_refused():
    with pytest.raises(auth.AuthError):
        auth.login_or_create_google(email="not-an-email")


# --- the migration -----------------------------------------------------------


def test_an_old_accounts_db_gains_the_new_columns_and_keeps_working():
    """A database created before Google support must upgrade in place."""
    import sqlite3
    import time

    from companies_research.config import SETTINGS

    db = SETTINGS.db_path.parent / "accounts.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db)
    conn.executescript(
        "CREATE TABLE users (id TEXT PRIMARY KEY, email TEXT UNIQUE, name TEXT, "
        "pw_hash TEXT NOT NULL, pw_salt TEXT NOT NULL, rounds INTEGER NOT NULL, "
        "created_at REAL NOT NULL, last_login_at REAL);"
        "CREATE TABLE sessions (token TEXT PRIMARY KEY, user_id TEXT, "
        "created_at REAL, expires_at REAL);")
    salt = auth.secrets.token_bytes(16)
    conn.execute("INSERT INTO users VALUES (?,?,?,?,?,?,?,?)",
                 ("u1", "old@example.com", "Old",
                  auth._hash("old-pass-8", salt, auth._PBKDF2_ROUNDS), salt.hex(),
                  auth._PBKDF2_ROUNDS, time.time(), None))
    conn.commit()
    conn.close()

    # first _connect() through the app runs the ALTER
    assert auth.authenticate(email="old@example.com", password="old-pass-8").id == "u1"
    cols = {r[1] for r in sqlite3.connect(db).execute("PRAGMA table_info(users)")}
    assert "auth_provider" in cols and "google_sub" in cols
    provider = sqlite3.connect(db).execute(
        "SELECT auth_provider FROM users WHERE id = 'u1'").fetchone()[0]
    assert provider == "local", "an existing row must backfill to local"


# --- the endpoints -----------------------------------------------------------


def test_google_endpoints_are_refused_on_a_non_local_host(monkeypatch, tmp_path):
    """The loopback-only gate: the public tunnel must not reach these."""
    monkeypatch.setenv("PUBLIC_HOSTS", "demo.trycloudflare.com")
    from companies_research.config import reload_settings
    reload_settings()
    from starlette.testclient import TestClient
    from companies_research.webapp import server

    client = TestClient(server.app, base_url="http://demo.trycloudflare.com")
    client.headers.update({"x-cr-token": server.TOKEN,
                           "origin": "http://demo.trycloudflare.com"})
    assert client.post("/api/auth/google/start").status_code == 404
    assert client.get("/api/auth/google/poll?job_id=x").status_code == 404
    assert client.post("/api/auth/google/finish", json={"job_id": "x"}).status_code == 404
    assert client.get("/api/auth/status").json()["google_login_available"] is False


def test_google_endpoints_still_require_the_run_token(monkeypatch, tmp_path):
    from starlette.testclient import TestClient
    from companies_research.config import reload_settings
    reload_settings()
    from companies_research.webapp import server

    client = TestClient(server.app, base_url="http://127.0.0.1:8765", client=("127.0.0.1", 50000))
    client.headers.update({"origin": "http://127.0.0.1:8765"})   # no token
    assert client.post("/api/auth/google/start").status_code == 401


def test_google_poll_cannot_read_a_non_auth_job(monkeypatch, tmp_path):
    """The public poll must not become a window onto seed/scan jobs."""
    from starlette.testclient import TestClient
    from companies_research.config import reload_settings
    reload_settings()
    from companies_research.webapp import server
    from companies_research.webapp.jobs import RUNNER

    other = RUNNER.start("scan", lambda progress: {"leads": 0})
    client = TestClient(server.app, base_url="http://127.0.0.1:8765", client=("127.0.0.1", 50000))
    client.headers.update({"x-cr-token": server.TOKEN, "origin": "http://127.0.0.1:8765"})
    assert client.get(f"/api/auth/google/poll?job_id={other.id}").status_code == 404


def test_finish_trades_a_completed_job_for_a_session(monkeypatch, tmp_path):
    """The whole point, without a browser: a done auth-google job → a session,
    with the email read from the job result, not the request body."""
    import time

    from starlette.testclient import TestClient
    from companies_research.config import reload_settings
    reload_settings()
    from companies_research.webapp import server
    from companies_research.webapp.jobs import RUNNER

    # a fast fake OAuth job — returns an identity without opening a browser
    job = RUNNER.start("auth-google", lambda progress: {"email": "chris@gmail.com"})
    job.secret = "test-secret"                       # start() sets this in prod
    for _ in range(50):
        if RUNNER.get(job.id).status != "running":
            break
        time.sleep(0.02)

    client = TestClient(server.app, base_url="http://127.0.0.1:8765", client=("127.0.0.1", 50000))
    client.headers.update({"x-cr-token": server.TOKEN, "origin": "http://127.0.0.1:8765"})

    # before: no session
    assert client.get("/api/state").status_code == 401
    finish = client.post("/api/auth/google/finish",
                         json={"job_id": job.id, "secret": "test-secret"})
    assert finish.status_code == 200, finish.text
    assert finish.json()["user"]["email"] == "chris@gmail.com"
    # after: the same client now carries a session and reaches a gated route
    assert client.get("/api/state").status_code == 200
    # single-use: the job is consumed, so a replay finds nothing to spend
    replay = client.post("/api/auth/google/finish",
                         json={"job_id": job.id, "secret": "test-secret"})
    assert replay.status_code == 404, "a spent sign-in was replayable"


def test_finish_rejects_a_body_supplied_email(monkeypatch, tmp_path):
    """A client must not be able to name the account it logs into."""
    import time

    from starlette.testclient import TestClient
    from companies_research.config import reload_settings
    reload_settings()
    from companies_research.webapp import server
    from companies_research.webapp.jobs import RUNNER

    job = RUNNER.start("auth-google", lambda progress: {"email": "owner@gmail.com"})
    job.secret = "test-secret"
    for _ in range(50):
        if RUNNER.get(job.id).status != "running":
            break
        time.sleep(0.02)
    client = TestClient(server.app, base_url="http://127.0.0.1:8765", client=("127.0.0.1", 50000))
    client.headers.update({"x-cr-token": server.TOKEN, "origin": "http://127.0.0.1:8765"})
    # attacker adds their own email to the body; the server must ignore it and
    # use the job result (owner@gmail.com)
    finish = client.post("/api/auth/google/finish",
                         json={"job_id": job.id, "secret": "test-secret",
                               "email": "attacker@evil.com"})
    assert finish.status_code == 200
    assert finish.json()["user"]["email"] == "owner@gmail.com"


# --- fixes from the security review -----------------------------------------


def test_finish_without_the_secret_is_refused(monkeypatch, tmp_path):
    """The nonce binds a sign-in to the browser that started it."""
    import time

    from starlette.testclient import TestClient
    from companies_research.config import reload_settings
    reload_settings()
    from companies_research.webapp import server
    from companies_research.webapp.jobs import RUNNER

    job = RUNNER.start("auth-google", lambda progress: {"email": "chris@gmail.com"})
    job.secret = "the-real-secret"
    for _ in range(50):
        if RUNNER.get(job.id).status != "running":
            break
        time.sleep(0.02)
    client = TestClient(server.app, base_url="http://127.0.0.1:8765", client=("127.0.0.1", 50000))
    client.headers.update({"x-cr-token": server.TOKEN, "origin": "http://127.0.0.1:8765"})
    # no secret, and a wrong secret, both look like "no such sign-in"
    assert client.post("/api/auth/google/finish", json={"job_id": job.id}).status_code == 404
    assert client.post("/api/auth/google/finish",
                       json={"job_id": job.id, "secret": "guess"}).status_code == 404
    assert client.get("/api/auth/google/poll?job_id=" + job.id).status_code == 404


def test_state_never_exposes_an_auth_google_job(monkeypatch, tmp_path):
    """The identity-bearing job id and email must not leak to signed-in users."""
    import time

    from starlette.testclient import TestClient
    from companies_research.config import reload_settings
    reload_settings()
    from companies_research.webapp import server
    from companies_research.webapp.jobs import RUNNER

    # a signed-in client
    client = TestClient(server.app, base_url="http://127.0.0.1:8765", client=("127.0.0.1", 50000))
    client.headers.update({"x-cr-token": server.TOKEN, "origin": "http://127.0.0.1:8765"})
    client.post("/api/auth/signup", json={"email": "a@b.com", "password": "demo-pass-8"})

    job = RUNNER.start("auth-google", lambda progress: {"email": "secret@gmail.com"})
    job.secret = "s"
    for _ in range(50):
        if RUNNER.get(job.id).status != "running":
            break
        time.sleep(0.02)
    state = client.get("/api/state").json()
    assert state["job"] is None, "an auth-google job leaked through /api/state"


def test_local_gate_ignores_a_spoofed_host_and_uses_the_peer(monkeypatch, tmp_path):
    """Host: 127.0.0.1 from a public deploy must not pass the local gate."""
    from companies_research.config import reload_settings

    # public deploy declared via PUBLIC_HOSTS → never local, whatever the Host says
    monkeypatch.setenv("PUBLIC_HOSTS", "demo.trycloudflare.com")
    reload_settings()
    from companies_research.webapp import server

    class _Req:
        def __init__(self, host_header, peer):
            self.headers = {"host": host_header}
            self.client = type("C", (), {"host": peer})()

    # spoofed loopback Host from a real remote peer, on a public deploy
    assert server._request_is_local(_Req("127.0.0.1", "203.0.113.9")) is False
    # even a loopback peer is refused while public_hosts is set (same-host proxy)
    assert server._request_is_local(_Req("127.0.0.1", "127.0.0.1")) is False

    # a genuine local run (no public hosts) with a loopback peer is allowed
    monkeypatch.delenv("PUBLIC_HOSTS", raising=False)
    reload_settings()
    assert server._request_is_local(_Req("anything", "127.0.0.1")) is True
    # ...but a remote peer is refused even with no public hosts and a spoofed Host
    assert server._request_is_local(_Req("127.0.0.1", "10.0.0.5")) is False


def test_concurrent_first_signups_yield_exactly_one_account(monkeypatch, tmp_path):
    """The lockdown TOCTOU: two racing signups must not both become 'the first'."""
    import threading

    from companies_research.config import reload_settings
    reload_settings()
    from companies_research.webapp import auth

    barrier = threading.Barrier(8)
    outcomes: list = []

    def signup(n):
        barrier.wait()   # release all threads at once to maximise the race
        try:
            auth.create_user(email=f"user{n}@example.com", password="demo-pass-8")
            outcomes.append("ok")
        except auth.AuthError:
            outcomes.append("blocked")

    threads = [threading.Thread(target=signup, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert outcomes.count("ok") == 1, f"lockdown let {outcomes.count('ok')} accounts through"
    assert auth.user_count() == 1


def test_the_migration_survives_concurrent_upgrades(monkeypatch, tmp_path):
    """Two connections racing the first ALTER must not 500 the loser."""
    import sqlite3
    import threading
    import time

    from companies_research.config import SETTINGS, reload_settings
    reload_settings()
    from companies_research.webapp import auth

    db = SETTINGS.db_path.parent / "accounts.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db)
    conn.executescript(
        "CREATE TABLE users (id TEXT PRIMARY KEY, email TEXT UNIQUE, name TEXT, "
        "pw_hash TEXT NOT NULL, pw_salt TEXT NOT NULL, rounds INTEGER NOT NULL, "
        "created_at REAL NOT NULL, last_login_at REAL);"
        "CREATE TABLE sessions (token TEXT PRIMARY KEY, user_id TEXT, "
        "created_at REAL, expires_at REAL);")
    conn.commit()
    conn.close()

    errors: list = []
    barrier = threading.Barrier(6)

    def connect_once():
        barrier.wait()
        try:
            auth._connect().close()
        except Exception as exc:   # noqa: BLE001
            errors.append(repr(exc))

    threads = [threading.Thread(target=connect_once) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"a concurrent upgrade raised: {errors}"
    cols = {r[1] for r in sqlite3.connect(db).execute("PRAGMA table_info(users)")}
    assert "auth_provider" in cols and "google_sub" in cols
