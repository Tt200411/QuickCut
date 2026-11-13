"""CLI 输入解析、输出重验与错误信封回归，全部使用本地夹具。"""
from __future__ import annotations

import base64
import copy
import io
import json
import subprocess
import sys
from pathlib import Path

import pytest
from PIL import Image

from quickcut.planning import cli
from quickcut.planning.selection import RuleSelector
from quickcut.planning.contracts import load_bundle, read_json, ref, write_json
from quickcut.planning.planning import build_outline, build_retrieval_request

ROOT = Path(__file__).resolve().parents[1]


def invoke(*args):
    completed = subprocess.run(
        [sys.executable, "-m", "quickcut.planning", *map(str, args)],
        cwd=ROOT, capture_output=True, text=True, check=False,
    )
    output = completed.stderr if completed.returncode == 2 else completed.stdout
    assert "Traceback" not in completed.stderr, completed.stderr
    return completed.returncode, json.loads(output)


@pytest.fixture
def custom_inputs(tmp_path):
    raw, docs = load_bundle(cli.DEFAULT_BUNDLE)
    candidates = read_json(cli.DEFAULT_C_CANDIDATES)
    candidates["document_id"] = "candidates-review"
    candidates["revision"] = 4
    request = build_retrieval_request(
        docs["Brief"], docs["AssetManifest"], build_outline(docs["Brief"]),
        document_id="request-review", revision=3,
    )
    # 改变查询 ID，确保覆盖的是请求正文，而不只是文档信封。
    for query in request["queries"]:
        query["query_id"] = "review-" + query["query_id"]
    for result in candidates["results"]:
        result["query_id"] = "review-" + result["query_id"]
    candidates["inputs"] = [ref(request), ref(docs["AssetManifest"])]
    bundle = write_json(tmp_path / "custom.json", raw)
    candidate_path = write_json(tmp_path / "candidates.json", candidates)
    request_path = write_json(tmp_path / "request.json", request)
    return raw, docs, candidates, request, bundle, candidate_path, request_path


@pytest.mark.parametrize("explicit_root", [False, True])
def test_run_parser_project_root(tmp_path, monkeypatch, explicit_root):
    captured = []

    def capture(args):
        captured.append(args)
        return 0

    monkeypatch.setattr(cli, "command_run", capture)
    options = ["--project-root", str(tmp_path)] if explicit_root else []
    assert cli.main(["run", *options]) == 0
    assert captured[0].project_root == str(tmp_path if explicit_root else cli.ROOT)


@pytest.mark.parametrize("explicit_root", [False, True])
def test_run_passes_resolved_project_root_to_ollama(tmp_path, monkeypatch, capsys, explicit_root):
    project = tmp_path / "project"
    project.mkdir()
    (tmp_path / "project-link").symlink_to(project, target_is_directory=True)
    monkeypatch.chdir(tmp_path)
    constructed = []

    def construct(**kwargs):
        constructed.append(kwargs)
        return RuleSelector()

    monkeypatch.setattr(cli, "OllamaSelector", construct)
    options = ["--project-root", "project-link"] if explicit_root else []
    assert cli.main(["run", "--mode", "ollama", *options, "--out-dir", str(tmp_path / "run")]) == 0
    assert constructed == [{"project_root": project.resolve() if explicit_root else cli.ROOT}]
    assert json.loads(capsys.readouterr().out)["validation_ok"] is True


@pytest.mark.parametrize("kind", [
    "relative", "default", "absolute", "parent", "internal_parent",
    "symlink", "directory_symlink", "internal_symlink",
])
def test_run_ollama_project_images_and_escape_guards(
    tmp_path, custom_inputs, monkeypatch, capsys, kind,
):
    raw, docs, _, _, bundle, candidate_path, request_path = custom_inputs
    project = tmp_path / "project"
    project.mkdir()
    assert not project.resolve().is_relative_to(cli.ROOT)
    image = project / "frame.png"
    Image.new("RGB", (2, 2), "green").save(image)
    outside = tmp_path / "outside.png"
    Image.new("RGB", (2, 2), "red").save(outside)
    uri = image.name
    if kind == "absolute":
        uri = str(outside)
    elif kind == "parent":
        uri = "../outside.png"
    elif kind == "internal_parent":
        (project / "subdir").mkdir()
        uri = "subdir/../frame.png"
    elif kind == "symlink":
        (project / "linked.png").symlink_to(outside)
        uri = "linked.png"
    elif kind == "directory_symlink":
        (project / "linked").symlink_to(tmp_path, target_is_directory=True)
        uri = "linked/outside.png"
    elif kind == "internal_symlink":
        (project / "linked.png").symlink_to(image)
        uri = "linked.png"
    for segment in docs["AssetManifest"]["segments"]:
        for frame in segment["keyframes"]:
            frame["uri"] = uri
    write_json(bundle, raw)
    requests = []

    def respond(request, timeout):
        payload = json.loads(request.data)
        requests.append(payload)
        prompt = json.loads(payload["prompt"])
        chosen = next(c for c in prompt["candidates"] if c["origin"] == "user")
        return io.BytesIO(json.dumps({"response": {
            "shot_order": [chosen["candidate_id"]],
            "reasons": {chosen["candidate_id"]: "用户事件证据"},
            "roles": {chosen["candidate_id"]: "primary"},
        }}).encode("utf-8"))

    monkeypatch.setattr("urllib.request.urlopen", respond)
    options = [] if kind == "default" else ["--project-root", str(project)]
    assert cli.main([
        "run", "--mode", "ollama", "--bundle", str(bundle),
        "--candidate-set", str(candidate_path), "--retrieval-request", str(request_path),
        *options, "--out-dir", str(tmp_path / "run"),
    ]) == 0
    assert json.loads(capsys.readouterr().out)["validation_ok"] is True
    assert requests
    accepted = kind in {"relative", "internal_symlink"}
    for payload in requests:
        prompt = json.loads(payload["prompt"])
        assert all(c["visual_evidence"] is accepted for c in prompt["candidates"])
        if accepted:
            assert prompt["image_mapping"]
            assert all(m["uri"] == uri for m in prompt["image_mapping"])
            assert payload["images"] == [
                base64.b64encode(image.read_bytes()).decode("ascii")
            ] * len(prompt["image_mapping"])
            for candidate in prompt["candidates"]:
                assert candidate["image_indices"] == [
                    m["image_index"] for m in prompt["image_mapping"]
                    if m["candidate_id"] == candidate["candidate_id"]
                ]
        else:
            assert "images" not in payload
            assert prompt["image_mapping"] == []
            assert all(c["image_indices"] == [] for c in prompt["candidates"])


@pytest.mark.parametrize("case", ["complete", "insufficient"])
def test_default_run_then_validate_and_saved_bundle(tmp_path, case):
    out = tmp_path / "run"
    rc, summary = invoke("run", "--case", case, "--out-dir", out)
    assert rc == 0, summary
    assert summary["validation_ok"] is True
    if case == "complete":
        rc, report = invoke("validate", out / "story_plan.json")
        assert rc == 0, report
        assert report["ok"] is True
    saved = Path(summary["bundle"])
    assert saved == out / "bundle.json"
    _, source_docs = load_bundle(cli.DEFAULT_BUNDLE)
    _, saved_docs = load_bundle(saved)
    assert saved_docs.keys() == source_docs.keys()
    assert saved_docs["StoryPlan"] == read_json(out / "story_plan.json")
    assert saved_docs["RetrievalRequest"] == read_json(out / "retrieval_request.json")
    assert saved_docs["CandidateSet"]["document_id"] == (
        "candidates-insufficient-001" if case == "insufficient"
        else read_json(cli.DEFAULT_C_CANDIDATES)["document_id"]
    )
    for kind in source_docs.keys() - {"StoryPlan", "RetrievalRequest", "CandidateSet"}:
        assert saved_docs[kind] == source_docs[kind]
    rc, report = invoke("validate", out / "story_plan.json", "--bundle", saved)
    assert rc == 0, report
    assert report["ok"] is True


def test_default_no_broll_keeps_rejecting_unmatched_candidate_results(tmp_path):
    rc, error = invoke("run", "--no-broll-queries", "--out-dir", tmp_path / "run")
    assert rc == 2, error
    assert error["code"] == "VLM_INVALID_PLAN"
    assert "逐一对应查询" in error["message"]


@pytest.mark.parametrize("explicit_bundle", [False, True])
@pytest.mark.parametrize("approved", [False, True])
def test_custom_candidate_and_request_overrides_round_trip(
    tmp_path, custom_inputs, explicit_bundle, approved,
):
    _, _, candidates, request, bundle, candidate_path, request_path = custom_inputs
    options = ["--candidate-set", candidate_path, "--retrieval-request", request_path]
    if explicit_bundle:
        options += ["--bundle", bundle]
    run_options = ["--approved-cues", cli.DEFAULT_APPROVED_CUES] if approved else []
    out = tmp_path / "run"
    rc, summary = invoke("run", *options, *run_options, "--out-dir", out)
    assert rc == 0, summary
    story = read_json(out / "story_plan.json")
    assert story["status"] == ("ready_for_edit" if approved else "needs_input")
    assert all(cue["approved"] is approved for cue in story["narration_cues"])
    assert ref(candidates) in story["inputs"]
    assert read_json(out / "retrieval_request.json") == request
    rc, report = invoke("validate", out / "story_plan.json", *options)
    assert rc == 0, report
    assert report["ok"] is True
    rc, report = invoke("validate", out / "story_plan.json", "--bundle", out / "bundle.json")
    assert rc == 0, report
    assert report["ok"] is True


@pytest.mark.parametrize("primary_only", [False, True])
def test_custom_no_broll_checks_request_content(tmp_path, custom_inputs, primary_only):
    _, _, candidates, request, bundle, candidate_path, request_path = custom_inputs
    if primary_only:
        request["queries"] = [q for q in request["queries"] if q["role"] == "primary"]
        query_ids = {q["query_id"] for q in request["queries"]}
        candidates["results"] = [r for r in candidates["results"] if r["query_id"] in query_ids]
        write_json(request_path, request)
        write_json(candidate_path, candidates)
    out = tmp_path / "no-broll"
    rc, result = invoke(
        "run", "--bundle", bundle, "--retrieval-request", request_path,
        "--candidate-set", candidate_path, "--no-broll-queries", "--out-dir", out,
    )
    if not primary_only:
        assert rc == 2 and result["code"] == "VLM_INVALID_PLAN", result
        return
    assert rc == 0 and result["validation_ok"], result
    story = read_json(out / "story_plan.json")
    assert story["status"] == "needs_input"
    assert all(s["origin"] == "user" for b in story["beats"] for s in b["shots"])
    assert all(c["approved"] is False for c in story["narration_cues"])
    rc, report = invoke("validate", out / "story_plan.json", "--bundle", out / "bundle.json")
    assert rc == 0 and report["ok"], report


def test_custom_bundle_uses_its_own_request_without_default_approval(tmp_path, custom_inputs):
    raw, _, candidates, request, bundle, _, _ = custom_inputs
    raw["documents"] = [
        {"CandidateSet": candidates, "RetrievalRequest": request}.get(doc["kind"], doc)
        for doc in raw["documents"]
    ]
    write_json(bundle, raw)
    out = tmp_path / "run"
    rc, summary = invoke("run", "--bundle", bundle, "--out-dir", out)
    assert rc == 0, summary
    assert summary["status"] == "needs_input"
    assert read_json(out / "retrieval_request.json") == request
    assert all(cue["approved"] is False for cue in read_json(out / "story_plan.json")["narration_cues"])
    rc, report = invoke("validate", out / "story_plan.json", "--bundle", bundle)
    assert rc == 0, report


@pytest.mark.parametrize("command", ["run", "validate"])
@pytest.mark.parametrize("field,value", [("document_id", "another-request"), ("revision", 99)])
def test_mismatched_candidate_request_reference_is_not_rebound(
    tmp_path, custom_inputs, command, field, value,
):
    _, _, _, request, bundle, candidate_path, request_path = custom_inputs
    request[field] = value
    write_json(request_path, request)
    args = [command]
    if command == "validate":
        args += [bundle]  # 在读取方案前就应拒绝不匹配的上游引用。
    else:
        args += ["--out-dir", tmp_path / "run"]
    rc, error = invoke(
        *args, "--bundle", bundle, "--candidate-set", candidate_path,
        "--retrieval-request", request_path,
    )
    assert rc == 2, error
    assert error["status"] == "failed"
    assert error["code"] == "STALE_INPUT_REVISION"


@pytest.mark.parametrize("command", ["run", "validate"])
def test_malformed_candidate_override_returns_json_error(tmp_path, command):
    candidates = read_json(cli.DEFAULT_C_CANDIDATES)
    candidates["inputs"] = [None]
    path = write_json(tmp_path / "malformed-candidates.json", candidates)
    args = [command, "--candidate-set", path]
    if command == "run":
        args += ["--out-dir", tmp_path / "run"]
    else:
        args += [cli.DEFAULT_BUNDLE]
    rc, error = invoke(*args)
    assert rc == 2, error
    assert error["code"] == "STALE_INPUT_REVISION"
    assert error["status"] == "failed"
    assert "inputs" in error["message"]


def test_candidate_override_does_not_synthesize_custom_request(tmp_path, custom_inputs):
    _, _, _, _, _, candidate_path, _ = custom_inputs
    rc, error = invoke("run", "--candidate-set", candidate_path, "--out-dir", tmp_path / "run")
    assert rc == 2, error
    assert error["code"] == "STALE_INPUT_REVISION"


def test_plan_from_different_candidate_document_is_rejected(tmp_path, custom_inputs):
    _, _, candidates, _, bundle, candidate_path, request_path = custom_inputs
    out = tmp_path / "run"
    options = ["--bundle", bundle, "--candidate-set", candidate_path, "--retrieval-request", request_path]
    rc, summary = invoke("run", *options, "--out-dir", out)
    assert rc == 0, summary
    candidates["document_id"] = "another-candidate-document"
    write_json(candidate_path, candidates)
    rc, report = invoke("validate", out / "story_plan.json", *options)
    assert rc == 1, report
    assert report["ok"] is False
    assert any("CandidateSet" in error for error in report["errors"])
    rc, report = invoke("validate", out / "story_plan.json")
    assert rc == 1, report
    assert report["ok"] is False


@pytest.mark.parametrize("command", ["run", "validate"])
@pytest.mark.parametrize("malformed", ["requirements", "candidate_inputs"])
def test_malformed_upstream_returns_json_error(tmp_path, command, malformed):
    raw, docs = load_bundle(cli.DEFAULT_BUNDLE)
    if malformed == "requirements":
        docs["Brief"]["requirements"] = None
    else:
        docs["CandidateSet"]["inputs"] = [None]
    bundle = write_json(tmp_path / "malformed.json", raw)
    args = [command, "--bundle", bundle]
    if command == "run":
        args += ["--out-dir", tmp_path / "run"]
    else:
        args += [bundle]
    rc, error = invoke(*args)
    assert rc == 2, error
    assert error["status"] == "failed"
    assert error["code"] in {"VLM_INVALID_PLAN", "STALE_INPUT_REVISION"}


@pytest.mark.parametrize("side", ["left", "right"])
@pytest.mark.parametrize("payload", [None, {"beats": [None]}])
def test_compare_malformed_json_returns_error_envelope(tmp_path, side, payload):
    paths = {name: write_json(tmp_path / f"{name}.json", {}) for name in ("left", "right")}
    write_json(paths[side], copy.deepcopy(payload))
    rc, error = invoke("compare", paths["left"], paths["right"])
    assert rc == 2, error
    assert error["status"] == "failed"
    assert error["code"] == "VLM_INVALID_PLAN"
