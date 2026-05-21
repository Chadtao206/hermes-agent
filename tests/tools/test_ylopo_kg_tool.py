import json
import sqlite3

from tools.registry import registry
from toolsets import get_toolset
import tools.ylopo_kg_tool as ykg
from tools.ylopo_kg_tool import (
    _build_fts_literal_query,
    ylopo_kg_deps,
    ylopo_kg_ingest,
    ylopo_kg_repo,
    ylopo_kg_search,
    ylopo_kg_stats,
)


def _init_schema(conn: sqlite3.Connection, *, with_fts: bool = True) -> None:
    conn.executescript(
        """
        CREATE TABLE kg_nodes (
            node_id INTEGER PRIMARY KEY,
            node_type TEXT,
            key TEXT,
            label TEXT,
            repo TEXT,
            path TEXT,
            attrs_json TEXT,
            created_at TEXT,
            updated_at TEXT
        );

        CREATE TABLE kg_edges (
            edge_id INTEGER PRIMARY KEY,
            src_node_id INTEGER,
            edge_type TEXT,
            dst_node_id INTEGER,
            weight REAL,
            attrs_json TEXT,
            source_repo TEXT,
            source_path TEXT,
            source_commit TEXT,
            created_at TEXT,
            updated_at TEXT
        );

        CREATE TABLE kg_docs (
            doc_id INTEGER PRIMARY KEY,
            node_id INTEGER,
            repo TEXT,
            path TEXT,
            commit_sha TEXT,
            doc_type TEXT,
            content_hash TEXT,
            content TEXT,
            attrs_json TEXT,
            updated_at TEXT
        );

        CREATE TABLE kg_ingest_state (
            repo_key TEXT PRIMARY KEY,
            repo_path TEXT,
            branch TEXT,
            head_commit TEXT,
            indexed_at TEXT,
            status TEXT,
            stats_json TEXT
        );
        """
    )
    if with_fts:
        conn.executescript(
            """
            CREATE VIRTUAL TABLE kg_docs_fts USING fts5(
                content,
                repo,
                path,
                doc_type,
                commit_sha,
                content='kg_docs',
                content_rowid='doc_id'
            );

            CREATE TRIGGER kg_docs_ai AFTER INSERT ON kg_docs BEGIN
                INSERT INTO kg_docs_fts(rowid, content, repo, path, doc_type, commit_sha)
                VALUES (new.doc_id, new.content, new.repo, new.path, new.doc_type, new.commit_sha);
            END;
            """
        )


def test_registry_and_toolset_registration():
    for tool_name in [
        "ylopo_kg_stats",
        "ylopo_kg_deps",
        "ylopo_kg_repo",
        "ylopo_kg_search",
        "ylopo_kg_ingest",
    ]:
        entry = registry.get_entry(tool_name)
        assert entry is not None
        assert entry.toolset == "ylopo_kg"

    ts = get_toolset("ylopo_kg")
    assert ts is not None
    assert "ylopo_kg_stats" in ts["tools"]


def test_build_fts_literal_query_quotes_terms():
    assert _build_fts_literal_query("alpha beta") == '"alpha" AND "beta"'
    assert _build_fts_literal_query("a\"b") == '"a""b"'


def test_ylopo_kg_deps_dedupes_by_source_repo_prefers_package(tmp_path):
    db = tmp_path / "kg.db"
    conn = sqlite3.connect(db)
    _init_schema(conn)

    conn.execute(
        "INSERT INTO kg_nodes VALUES (1,'package','package:@ylopo/target','@ylopo/target',NULL,NULL,'{}','t','t')"
    )
    conn.execute(
        "INSERT INTO kg_nodes VALUES (2,'repo','repo:repo-a','repo-a','repo-a','.', '{}','t','t')"
    )
    conn.execute(
        "INSERT INTO kg_nodes VALUES (3,'package','package:@ylopo/repo-a','@ylopo/repo-a','repo-a',NULL,'{}','t','t')"
    )

    attrs = json.dumps({"dep_type": "dependencies", "spec": "^1.2.3", "internal_ylopo": True}, separators=(",", ":"))
    conn.execute(
        "INSERT INTO kg_edges VALUES (1,2,'depends_on',1,1.0,?,'repo-a','package.json','abcdef123456','t','t')",
        (attrs,),
    )
    conn.execute(
        "INSERT INTO kg_edges VALUES (2,3,'depends_on',1,1.0,?,'repo-a','package.json','abcdef123456','t','t')",
        (attrs,),
    )
    conn.commit()
    conn.close()

    result = json.loads(ylopo_kg_deps(package_name="@ylopo/target", db=str(db), limit=10))
    assert result["success"] is True
    assert result["count"] == 1
    dep = result["dependents"][0]
    assert dep["source_type"] == "package"
    assert dep["source_repo"] == "repo-a"
    assert dep["dep_type"] == "dependencies"
    assert dep["spec"] == "^1.2.3"
    assert dep["internal_ylopo"] is True


def test_ylopo_kg_repo_returns_internal_dependencies_only(tmp_path):
    db = tmp_path / "kg.db"
    conn = sqlite3.connect(db)
    _init_schema(conn)

    conn.execute(
        "INSERT INTO kg_nodes VALUES (1,'repo','repo:repo-a','repo-a','repo-a','.', '{}','t','2026-01-01T00:00:00Z')"
    )
    conn.execute(
        "INSERT INTO kg_nodes VALUES (2,'package','package:@ylopo/shared','@ylopo/shared',NULL,NULL,'{}','t','t')"
    )
    conn.execute(
        "INSERT INTO kg_nodes VALUES (3,'package','package:lodash','lodash',NULL,NULL,'{}','t','t')"
    )
    conn.execute(
        "INSERT INTO kg_nodes VALUES (4,'package','package:react','react',NULL,NULL,'{}','t','t')"
    )

    attrs_internal = json.dumps({"internal_ylopo": True}, separators=(",", ":"))
    attrs_external = json.dumps({"internal_ylopo": False}, separators=(",", ":"))
    conn.execute(
        "INSERT INTO kg_edges VALUES (1,1,'depends_on',2,1.0,?,'repo-a','package.json','abc','t','t')",
        (attrs_external,),
    )
    conn.execute(
        "INSERT INTO kg_edges VALUES (2,1,'depends_on',3,1.0,?,'repo-a','package.json','abc','t','t')",
        (attrs_internal,),
    )
    conn.execute(
        "INSERT INTO kg_edges VALUES (3,1,'depends_on',4,1.0,?,'repo-a','package.json','abc','t','t')",
        (attrs_external,),
    )
    conn.commit()
    conn.close()

    result = json.loads(ylopo_kg_repo(name="repo-a", db=str(db), limit=10))
    assert result["success"] is True
    labels = [d["dep_label"] for d in result["internal_dependencies"]]
    assert "@ylopo/shared" in labels
    assert "lodash" in labels
    assert "react" not in labels


def test_ylopo_kg_search_falls_back_to_like_without_fts(tmp_path):
    db = tmp_path / "kg.db"
    conn = sqlite3.connect(db)
    _init_schema(conn, with_fts=False)

    conn.execute(
        "INSERT INTO kg_docs VALUES (1,1,'repo-a','README.md','abcd','readme','h','hello ylopo graph world','{}','2026-01-01T00:00:00Z')"
    )
    conn.commit()
    conn.close()

    result = json.loads(ylopo_kg_search(query="ylopo graph", db=str(db), limit=5))
    assert result["success"] is True
    assert result["mode"] == "like"
    assert result["count"] == 1
    assert result["matches"][0]["repo"] == "repo-a"


def test_ylopo_kg_stats_reports_counts_and_health(tmp_path):
    db = tmp_path / "kg.db"
    conn = sqlite3.connect(db)
    _init_schema(conn)

    conn.execute(
        "INSERT INTO kg_nodes VALUES (1,'repo','repo:repo-a','repo-a','repo-a','.', '{}','t','t')"
    )
    conn.execute(
        "INSERT INTO kg_nodes VALUES (2,'package','package:@ylopo/a','@ylopo/a','repo-a',NULL,'{}','t','t')"
    )
    conn.execute(
        "INSERT INTO kg_edges VALUES (1,1,'depends_on',2,1.0,'{}','repo-a','package.json','abc','t','t')"
    )
    conn.execute(
        "INSERT INTO kg_docs VALUES (1,1,'repo-a','README.md','abc','readme','h','content','{}','2026-01-01T00:00:00Z')"
    )
    conn.execute(
        "INSERT INTO kg_ingest_state VALUES ('repo-a','/tmp/repo-a','main','abcdef123456','2026-01-02T00:00:00Z','ok','{}')"
    )
    conn.commit()
    conn.close()

    result = json.loads(ylopo_kg_stats(db=str(db)))
    assert result["success"] is True
    assert result["counts"]["repos"] == 1
    assert result["counts"]["packages"] == 1
    assert result["counts"]["dependencies"] == 1
    assert result["counts"]["docs"] == 1
    assert result["ingest_health"]["status"] == "healthy"


def test_ylopo_kg_ingest_uses_explicit_argv_and_bounds_output(monkeypatch, tmp_path):
    script = tmp_path / "ylopo_kg.py"
    script.write_text("#!/usr/bin/env python3\n")
    monkeypatch.setattr(ykg, "KG_SCRIPT_PATH", str(script))

    calls = []

    class Proc:
        returncode = 0
        stdout = "\n".join(f"line-{i}" for i in range(50))
        stderr = ""

    def fake_run(cmd, stdout, stderr, text, check):
        calls.append({
            "cmd": cmd,
            "stdout": stdout,
            "stderr": stderr,
            "text": text,
            "check": check,
        })
        return Proc()

    monkeypatch.setattr(ykg.subprocess, "run", fake_run)

    result = json.loads(ylopo_kg_ingest(root="/tmp/root", db=str(tmp_path / "kg.db")))

    assert result["success"] is True
    assert result["returncode"] == 0
    assert calls and isinstance(calls[0]["cmd"], list)
    assert calls[0]["cmd"][1] == str(script)
    assert calls[0]["cmd"][2:5] == ["ingest", "--root", "/tmp/root"]
    assert calls[0]["check"] is False
    assert len(result["stdout_tail"]) == 40
    assert result["stdout_tail"][0] == "line-10"


def test_ylopo_kg_ingest_reports_subprocess_exception(monkeypatch, tmp_path):
    script = tmp_path / "ylopo_kg.py"
    script.write_text("#!/usr/bin/env python3\n")
    monkeypatch.setattr(ykg, "KG_SCRIPT_PATH", str(script))

    def fake_run(*args, **kwargs):
        raise OSError("boom")

    monkeypatch.setattr(ykg.subprocess, "run", fake_run)

    result = json.loads(ylopo_kg_ingest(root="/tmp/root", db=str(tmp_path / "kg.db")))
    assert "error" in result
    assert "boom" in result["error"]


def test_ylopo_kg_toolset_resolves_when_explicitly_enabled_for_platform():
    from hermes_cli.tools_config import _get_platform_tools

    enabled = _get_platform_tools({"platform_toolsets": {"cli": ["ylopo_kg"]}}, "cli")
    assert "ylopo_kg" in enabled
