from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from element_mcp.config import ServerSettings
from element_mcp.graph import ProjectGraphService, _stable_id
from element_mcp.project import ProjectService


def graph_service(tmp_path: Path, project_path: Path | None) -> ProjectGraphService:
    settings = ServerSettings(
        project_path=project_path,
        config_path=tmp_path / "config.json",
        data_path=tmp_path / "data",
    )
    return ProjectGraphService(ProjectService(settings))


def append(path: Path, value: str) -> None:
    path.write_text(path.read_text(encoding="utf-8") + value, encoding="utf-8")


def test_dependency_graph_distinguishes_structural_yaml_and_lexical_evidence(
    tmp_path: Path,
    element_project_path: Path,
) -> None:
    append(element_project_path / "Sales" / "Orders.yaml", "Handler: FindOrder\nType: OrderDto\n")
    append(element_project_path / "Sales" / "Types" / "OrderDto.xbsl", "// Orders\nreturn Orders\n")
    graph = graph_service(tmp_path, element_project_path)

    explicit = graph.get_element_dependencies("Orders", direction="outgoing", depth=2)
    lexical = graph.get_element_dependencies(
        "OrderDto",
        direction="outgoing",
        depth=1,
        include_lexical=True,
    )

    explicit_types = {edge["type"] for edge in explicit["edges"]}
    assert {"metadata_file", "companion_file", "belongs_to_subsystem", "environment", "visibility"} <= explicit_types
    assert "yaml_handler" in explicit_types
    assert "yaml_type_reference" in explicit_types
    assert all(edge["evidence"]["path"] for edge in explicit["edges"])
    lexical_edges = [edge for edge in lexical["edges"] if edge["type"] == "lexical_reference"]
    assert len(lexical_edges) == 1
    assert lexical_edges[0]["confidence"] == "medium"
    assert lexical_edges[0]["resolution"] == "lexical_name_match"
    assert lexical["semantic_guarantee"] is False


def test_project_graph_filters_lexical_edges_and_reports_cycles(
    tmp_path: Path,
    element_project_path: Path,
) -> None:
    orders = element_project_path / "Sales" / "Orders.yaml"
    order_dto = element_project_path / "Sales" / "Types" / "OrderDto.yaml"
    orders.write_text(
        orders.read_text(encoding="utf-8").replace(
            "22222222-2222-2222-2222-222222222222",
            "22222222-2222-4222-8222-222222222222",
        ),
        encoding="utf-8",
    )
    order_dto.write_text(
        order_dto.read_text(encoding="utf-8").replace(
            "33333333-3333-3333-3333-333333333333",
            "33333333-3333-4333-8333-333333333333",
        ),
        encoding="utf-8",
    )
    append(
        orders,
        "Related: 33333333-3333-4333-8333-333333333333\n",
    )
    append(
        order_dto,
        "Related: 22222222-2222-4222-8222-222222222222\n",
    )
    append(element_project_path / "Sales" / "Types" / "OrderDto.xbsl", "return Orders\n")
    graph = graph_service(tmp_path, element_project_path)

    without_lexical = graph.get_project_dependency_graph(include_lexical=False)
    with_lexical = graph.get_project_dependency_graph(include_lexical=True)

    assert "lexical_reference" not in {edge["type"] for edge in without_lexical["edges"]}
    assert "lexical_reference" in {edge["type"] for edge in with_lexical["edges"]}
    assert {edge["type"] for edge in without_lexical["edges"]} == {"yaml_id_reference"}
    assert without_lexical["cycles"]


def test_change_impact_uses_reverse_edges_and_respects_limits(
    tmp_path: Path,
    element_project_path: Path,
) -> None:
    append(element_project_path / "Sales" / "Types" / "OrderDto.xbsl", "return Orders\n")
    graph = graph_service(tmp_path, element_project_path)

    impact = graph.analyze_change_impact(element="Orders", depth=2, include_lexical=True)
    bounded = graph.get_element_dependencies("Orders", limit=1)

    assert {item["name"] for item in impact["affected_elements"]} == {"OrderDto"}
    assert impact["semantic_guarantee"] is False
    assert bounded["count"] == 1
    assert bounded["truncated"] is True


def test_change_impact_requests_an_input_outside_git(
    tmp_path: Path,
    element_project_path: Path,
) -> None:
    result = graph_service(tmp_path, element_project_path).analyze_change_impact()

    assert result["status"] == "input_required"
    assert result["changed_elements"]["status"] == "unavailable"


def test_incremental_cache_reuses_unchanged_file_facts(
    tmp_path: Path,
    element_project_path: Path,
) -> None:
    graph = graph_service(tmp_path, element_project_path)

    first = graph.validate_element_structure()
    second = graph.validate_element_structure()
    append(element_project_path / "Sales" / "Orders.xbsl", "\n// changed\n")
    third = graph.validate_element_structure()

    assert first["graph"]["incremental"]["parsed_files"] > 0
    assert second["graph"]["cache_hit"] is True
    assert third["graph"]["cache_hit"] is False
    assert third["graph"]["incremental"]["parsed_files"] == 1
    assert third["graph"]["incremental"]["reused_files"] > 0


def test_structure_validation_reports_missing_handlers_ambiguity_and_orphans(
    tmp_path: Path,
    element_project_path: Path,
) -> None:
    append(element_project_path / "Sales" / "Orders.yaml", "Handler: MissingHandler\n")
    duplicate = element_project_path / "Sales" / "Types" / "Duplicate.yaml"
    duplicate.write_text("ElementKind: Structure\nName: Orders\n", encoding="utf-8")
    graph = graph_service(tmp_path, element_project_path)

    result = graph.validate_element_structure()
    codes = {issue["code"] for issue in result["issues"]}

    assert result["status"] == "invalid"
    assert {"missing_handler", "ambiguous_element_name", "orphan_source_file"} <= codes


def test_graph_does_not_follow_symlinks_outside_project(
    tmp_path: Path,
    element_project_path: Path,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "Outside.yaml").write_text("ElementKind: Structure\nName: Outside\n", encoding="utf-8")
    link = element_project_path / "linked"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Symlinks are unavailable in this environment")

    result = graph_service(tmp_path, element_project_path).get_project_dependency_graph(limit=200)

    assert "Outside" not in {node["name"] for node in result["nodes"]}


def test_explicit_changed_paths_map_files_and_deleted_metadata(
    tmp_path: Path,
    element_project_path: Path,
) -> None:
    graph = graph_service(tmp_path, element_project_path)

    result = graph.get_changed_elements(["Sales/Orders.xbsl", "Sales/Deleted.yaml"])

    assert result["source"] == "explicit_paths"
    assert result["changes"][0]["element"]["name"] == "Orders"
    assert result["changes"][0]["mapping"] == "exact_graph_file_owner"
    assert result["changes"][1]["element"]["name"] == "Deleted"
    assert result["changes"][1]["mapping"] == "deleted_metadata_path_inference"
    with pytest.raises(ValueError, match="внутри активного проекта"):
        graph.get_changed_elements(["../secret.yaml"])
    with pytest.raises(ValueError, match="не более 200"):
        graph.get_changed_elements([f"Sales/{index}.yaml" for index in range(201)])


@pytest.mark.skipif(shutil.which("git") is None, reason="Git is unavailable")
def test_local_git_status_maps_modified_untracked_and_renamed_files(
    tmp_path: Path,
    element_project_path: Path,
) -> None:
    subprocess.run(["git", "init", "-q"], cwd=element_project_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=element_project_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=element_project_path, check=True)
    subprocess.run(["git", "add", "."], cwd=element_project_path, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=element_project_path, check=True)
    append(element_project_path / "Sales" / "Orders.xbsl", "\n// changed\n")
    (element_project_path / "Sales" / "New.yaml").write_text(
        "ElementKind: Structure\nName: New\n",
        encoding="utf-8",
    )
    subprocess.run(
        ["git", "mv", "Sales/Types/OrderDto.xbsl", "Sales/Types/Renamed.xbsl"],
        cwd=element_project_path,
        check=True,
    )
    graph = graph_service(tmp_path, element_project_path)

    result = graph.get_changed_elements()

    by_status = {item["status"] for item in result["changes"]}
    assert result["source"] == "local_git_status"
    assert {"modified", "untracked", "renamed"} <= by_status
    renamed = next(item for item in result["changes"] if item["status"] == "renamed")
    assert renamed["path"] == "Sales/Types/Renamed.xbsl"
    assert renamed["old_path"] == "Sales/Types/OrderDto.xbsl"


def test_ide_git_summary_requires_paths_and_never_starts_second_git(
    tmp_path: Path,
    element_project_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = ProjectService(
        ServerSettings(config_path=tmp_path / "config.json", data_path=tmp_path / "data")
    )
    context = project.prepare_ide_workspace(
        {
            "workspace_folders": [str(element_project_path)],
            "git_status": {"modified": True, "branchName": "main"},
        }
    )
    project.activate_ide_workspace(context)
    graph = ProjectGraphService(project)
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: pytest.fail("Git must not run in IDE mode"))

    summary = graph.get_changed_elements()
    explicit = graph.get_changed_elements(["Sales/Orders.yaml"])

    assert summary["status"] == "paths_required"
    assert summary["source"] == "ide_git_summary"
    assert explicit["status"] == "ready"
    assert explicit["changes"][0]["element"]["name"] == "Orders"


def test_unused_candidates_are_low_confidence_and_ambiguous_selection_is_preserved(
    tmp_path: Path,
    element_project_path: Path,
) -> None:
    duplicate = element_project_path / "Sales" / "Types" / "Duplicate.yaml"
    duplicate.write_text("ElementKind: Structure\nName: Orders\n", encoding="utf-8")
    graph = graph_service(tmp_path, element_project_path)

    ambiguous = graph.get_element_dependencies("Orders")
    unused = graph.find_unused_project_elements(include_public=True)

    assert ambiguous["status"] == "selection_required"
    assert len(ambiguous["candidates"]) == 2
    assert unused["candidates"]
    assert all(item["confidence"] == "low" and item["requires_review"] for item in unused["candidates"])


def test_cycle_detection_handles_a_large_graph_without_python_recursion() -> None:
    count = 2500
    node_ids = [_stable_id("element", index) for index in range(count)]
    nodes = {node_id: {"id": node_id, "type": "element"} for node_id in node_ids}
    edges = [
        {"source": node_ids[index], "target": node_ids[(index + 1) % count], "type": "yaml_id_reference"}
        for index in range(count)
    ]

    cycles = ProjectGraphService._find_cycles(nodes, edges)

    assert len(cycles) == 1
    assert len(cycles[0]) == count
