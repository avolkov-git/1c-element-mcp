from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from element_mcp.config import ServerSettings
from element_mcp.documentation import DocumentationService
from element_mcp.project import ProjectError, ProjectService
from element_mcp.semantic import SemanticService


def service(tmp_path: Path, project_path: Path | None = None) -> ProjectService:
    return ProjectService(
        ServerSettings(
            project_path=project_path,
            config_path=tmp_path / "config.json",
            data_path=tmp_path / "data",
        )
    )


def semantic_service(
    tmp_path: Path,
    element_project_path: Path,
    corpus_path: Path,
) -> SemanticService:
    settings = ServerSettings(
        project_path=element_project_path,
        corpus_path=corpus_path,
        config_path=tmp_path / "config.json",
        data_path=tmp_path / "data",
    )
    return SemanticService(ProjectService(settings), DocumentationService(settings))


def test_project_status_is_missing_before_connection(tmp_path: Path) -> None:
    assert service(tmp_path).project_status() == {
        "status": "missing",
        "message": "Проект 1С:Предприятие.Элемент не подключён",
        "path": None,
        "source": None,
    }


def test_connect_and_overview_are_element_aware(tmp_path: Path, element_project_path: Path) -> None:
    project = service(tmp_path)

    connected = project.connect(element_project_path)
    overview = project.overview()

    assert connected["status"] == "ready"
    assert connected["project"]["name"] == "ExampleProject"
    assert project.configuration.active_project_path() == element_project_path.resolve()
    assert overview["project"]["version"] == "1.2.3"
    assert overview["elements"]["total"] == 4
    assert overview["elements"]["by_kind"] == {
        "CommonModule": 1,
        "Project": 1,
        "Structure": 1,
        "Subsystem": 1,
    }
    assert overview["subsystems"] == [{"name": "Sales", "path": "Sales"}]


def test_list_elements_returns_metadata_and_companion_modules(tmp_path: Path, element_project_path: Path) -> None:
    project = service(tmp_path, element_project_path)

    result = project.list_elements(query="orders", element_kind="CommonModule")

    assert result["total"] == 1
    assert result["next_offset"] is None
    assert result["elements"] == [
        {
            "name": "Orders",
            "element_kind": "CommonModule",
            "id": "22222222-2222-2222-2222-222222222222",
            "environment": "Server",
            "visibility_scope": "InProject",
            "subsystem": "Sales",
            "metadata_path": "Sales/Orders.yaml",
            "implementation_files": ["Sales/Orders.xbsl"],
        }
    ]


def test_search_and_bounded_file_read(tmp_path: Path, element_project_path: Path) -> None:
    project = service(tmp_path, element_project_path)

    search = project.search("FindOrder", file_type="xbsl")
    read = project.read_file("Sales/Orders.xbsl", start_line=1, line_count=2)

    assert search["count"] == 1
    assert search["results"][0]["path"] == "Sales/Orders.xbsl"
    assert search["results"][0]["line"] == 1
    assert read["path"] == "Sales/Orders.xbsl"
    assert read["content"] == "method FindOrder(Number: String): String\n    return Number"
    assert read["truncated"] is True


def test_unrelated_yaml_is_not_exposed_as_element_source(tmp_path: Path, element_project_path: Path) -> None:
    project = service(tmp_path, element_project_path)

    assert project.search("SECRET_VALUE")["count"] == 0
    with pytest.raises(ProjectError, match="не является метаданными элемента"):
        project.read_file("gitflic-ci.yaml")


def test_project_file_access_cannot_escape_root(tmp_path: Path, element_project_path: Path) -> None:
    project = service(tmp_path, element_project_path)
    outside = element_project_path.parent / "secret.yaml"
    outside.write_text("secret: value\n", encoding="utf-8")

    with pytest.raises(ProjectError, match="за пределами активного проекта"):
        project.read_file("../secret.yaml")

    with pytest.raises(ProjectError, match="только исходные файлы Element"):
        project.read_file("Project.xprj")


def test_connect_rejects_a_regular_directory(tmp_path: Path) -> None:
    directory = tmp_path / "not-element"
    directory.mkdir()

    with pytest.raises(ProjectError, match="Project.yaml"):
        service(tmp_path).connect(directory)


def test_ide_workspace_uses_official_git_context_and_requires_project_selection(
    tmp_path: Path,
    element_project_path: Path,
) -> None:
    workspace = tmp_path / "repository"
    first = workspace / "ActiveDirectory"
    second = workspace / "ActiveDirectoryTestApp"
    shutil.copytree(element_project_path, first)
    shutil.copytree(element_project_path, second)
    (first / "Project.yaml").write_text("Id: 1\nName: ActiveDirectory\n", encoding="utf-8")
    (second / "Project.yaml").write_text("Id: 2\nName: ActiveDirectoryTestApp\n", encoding="utf-8")
    project = service(tmp_path)

    prepared = project.prepare_ide_workspace(
        {
            "workspace_folders": [str(workspace)],
            "project_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            "git_status": {
                "commitId": "abc123",
                "branchName": "main",
                "modified": False,
                "aheadBehind": {"ahead": 1, "behind": 2},
                "commandStatus": "noConflict",
                "currentHead": "abc123",
                "commitMessage": "Не передавать это поле агенту",
            },
        }
    )
    context = project.activate_ide_workspace(prepared)
    status = project.project_status()

    assert status["status"] == "selection_required"
    assert [item["project"]["name"] for item in context["candidates"]] == [
        "ActiveDirectory",
        "ActiveDirectoryTestApp",
    ]
    assert context["git"] == {
        "source": "g5rt.team.status",
        "commit_id": "abc123",
        "branch_name": "main",
        "command_status": "noConflict",
        "current_head": "abc123",
        "modified": False,
        "ahead_behind": {"ahead": 1, "behind": 2},
    }
    assert "commitMessage" not in str(context)

    connected = project.connect(first)
    assert connected["source"] == "ide_session"
    assert project.configuration.active_project_path() is None
    assert project.project_status()["path"] == str(first.resolve())

    refreshed = project.prepare_ide_workspace(
        {
            "workspace_folders": [str(workspace)],
            "project_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            "git_status": {"branchName": "feature", "modified": True},
        }
    )
    project.activate_ide_workspace(refreshed)
    assert project.project_status()["path"] == str(first.resolve())
    assert project.project_status()["ide_context"]["git"]["branch_name"] == "feature"

    project.clear_ide_workspace()
    assert project.project_status()["status"] == "missing"


def test_ide_workspace_auto_selects_sole_project_and_matches_console(
    tmp_path: Path,
    element_project_path: Path,
) -> None:
    workspace = tmp_path / "repository"
    local_project = workspace / "ExampleProject"
    shutil.copytree(element_project_path, local_project)
    project = service(tmp_path)
    project.activate_ide_workspace(
        project.prepare_ide_workspace(
            {
                "workspace_folders": [str(workspace)],
                "project_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            }
        )
    )

    status = project.project_status()
    match = project.match_console_project(
        {
            "id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            "name": "ExampleProject",
            "presentation": "Example project",
            "code": "example",
        }
    )

    assert status["status"] == "ready"
    assert status["source"] == "ide_session"
    assert match["status"] == "ready"
    assert match["confirmation_required"] is False
    assert match["match_reason"] == "ide_session_selection"


def test_console_name_match_is_only_a_confirmation_suggestion(
    tmp_path: Path,
    element_project_path: Path,
) -> None:
    workspace = tmp_path / "repository"
    local_project = workspace / "ExampleProject"
    shutil.copytree(element_project_path, local_project)
    project = service(tmp_path)

    match = project.match_console_project(
        {"id": "project-id", "name": "ExampleProject", "presentation": "Example project", "code": "example"},
        workspace,
    )

    assert match["status"] == "confirmation_required"
    assert match["match_reason"] == "exact_name"
    assert match["suggestion"]["path"] == str(local_project.resolve())
    assert project.project_status()["status"] == "missing"


def test_russian_development_language_filenames_are_supported(tmp_path: Path) -> None:
    root = tmp_path / "russian-project"
    subsystem = root / "Основное"
    subsystem.mkdir(parents=True)
    (root / "Проект.yaml").write_text("Имя: Пример\nВерсия: 1.0.0\n", encoding="utf-8")
    (root / "Проект.xbsl").write_text("импорт Основное\n", encoding="utf-8")
    (subsystem / "Подсистема.yaml").write_text("Использование: []\n", encoding="utf-8")
    (subsystem / "Сервис.yaml").write_text(
        "ВидЭлемента: ОбщийМодуль\nИмя: Сервис\nОкружение: Сервер\n",
        encoding="utf-8",
    )
    (subsystem / "Сервис.xbsl").write_text("метод Выполнить()\n;\n", encoding="utf-8")

    project = service(tmp_path, root)
    overview = project.overview()
    elements = project.list_elements(query="Сервис")

    assert overview["project"]["manifest"] == "Проект.yaml"
    assert overview["project"]["name"] == "Пример"
    assert overview["subsystems"] == [{"name": "Основное", "path": "Основное"}]
    assert elements["elements"][0]["element_kind"] == "ОбщийМодуль"
    assert elements["elements"][0]["implementation_files"] == ["Основное/Сервис.xbsl"]


def test_lookup_symbol_returns_element_and_xbsl_declarations(
    tmp_path: Path,
    element_project_path: Path,
    corpus_path: Path,
) -> None:
    semantic = semantic_service(tmp_path, element_project_path, corpus_path)

    element = semantic.lookup_symbol("Orders")
    method = semantic.lookup_symbol("FindOrder", symbol_kind="method")

    assert element["resolution"] == "exact"
    assert element["semantic_guarantee"] is False
    assert element["matches"][0]["symbol_kind"] == "element"
    assert element["matches"][0]["declaration"] == {
        "path": "Sales/Orders.yaml",
        "line": 3,
        "column": 7,
        "text": "Name: Orders",
    }
    assert method["resolution"] == "exact"
    assert method["matches"][0]["declaration"]["path"] == "Sales/Orders.xbsl"
    assert method["matches"][0]["element"]["element_kind"] == "CommonModule"


def test_lookup_symbol_preserves_overloads_and_partial_search(
    tmp_path: Path,
    element_project_path: Path,
    corpus_path: Path,
) -> None:
    module = element_project_path / "Sales" / "Orders.xbsl"
    module.write_text(
        "method FindOrder(Number: String): String\n"
        "    return Number\n"
        ";\n"
        "method FindOrder(Number: Number): String\n"
        "    return Number.ToString()\n"
        ";\n",
        encoding="utf-8",
    )
    semantic = semantic_service(tmp_path, element_project_path, corpus_path)

    exact = semantic.lookup_symbol("FindOrder")
    partial = semantic.lookup_symbol("Order", exact=False)

    assert exact["resolution"] == "ambiguous"
    assert exact["total"] == 2
    assert len({item["symbol_id"] for item in exact["matches"]}) == 2
    assert {item["name"] for item in partial["matches"]} >= {"FindOrder", "Orders", "OrderDto"}


def test_find_references_uses_identifier_boundaries_and_reports_lexical_limit(
    tmp_path: Path,
    element_project_path: Path,
    corpus_path: Path,
) -> None:
    semantic = semantic_service(tmp_path, element_project_path, corpus_path)

    result = semantic.find_references("Orders", include_declarations=True)

    assert result["declaration_count"] == 1
    assert result["reference_count"] == 1
    assert result["semantic_guarantee"] is False
    assert [(item["role"], item["path"]) for item in result["results"]] == [
        ("declaration", "Sales/Orders.yaml"),
        ("reference", "Sales/OrdersQueries.xbql"),
    ]
    assert result["results"][1]["confidence"] == "medium"


def test_find_references_excludes_comments_strings_and_honors_file_scope(
    tmp_path: Path,
    element_project_path: Path,
    corpus_path: Path,
) -> None:
    module = element_project_path / "Sales" / "Orders.xbsl"
    module.write_text(
        "method FindOrder(Number: String): String\n"
        "    // FindOrder is mentioned in a comment\n"
        "    /* FindOrder is mentioned in\n"
        "       method FindOrder(Comment: String) */\n"
        '    val Message = "FindOrder in\n'
        '        a multiline string"\n'
        "    return FindOrder(Number) // FindOrder again\n"
        ";\n",
        encoding="utf-8",
    )
    semantic = semantic_service(tmp_path, element_project_path, corpus_path)

    result = semantic.find_references("FindOrder", relative_path="Sales/Orders.xbsl")

    assert result["declaration_count"] == 1
    assert result["reference_count"] == 1
    assert result["count"] == 1
    assert result["results"][0]["line"] == 7


def test_string_interpolation_does_not_hide_following_declarations(
    tmp_path: Path,
    element_project_path: Path,
    corpus_path: Path,
) -> None:
    module = element_project_path / "Sales" / "Orders.xbsl"
    module.write_text(
        "method BeforeInterpolation()\n"
        '    val Message = "Text ${Flag ? "yes" : "no"}"\n'
        ";\n"
        "method AfterInterpolation()\n"
        ";\n",
        encoding="utf-8",
    )
    semantic = semantic_service(tmp_path, element_project_path, corpus_path)

    declaration = semantic.lookup_symbol("AfterInterpolation")
    expression = semantic.find_references("Flag", relative_path="Sales/Orders.xbsl")

    assert declaration["resolution"] == "exact"
    assert declaration["matches"][0]["declaration"]["line"] == 4
    assert expression["reference_count"] == 1
    assert expression["results"][0]["line"] == 2


def test_resource_payload_yaml_is_not_treated_as_project_metadata(
    tmp_path: Path,
    element_project_path: Path,
) -> None:
    resources = element_project_path / "Sales" / "Resources"
    resources.mkdir()
    (resources / "Resources.yaml").write_text("VisibilityScope: InSubsystem\n", encoding="utf-8")
    (resources / "Template.yaml").write_text(
        "ElementKind: Structure\nName: Orders\nSecretMarker: value\n",
        encoding="utf-8",
    )
    (resources / "Template.xbsl").write_text("method EmbeddedTemplate()\n;\n", encoding="utf-8")
    project = service(tmp_path, element_project_path)

    overview = project.overview()
    templates = project.list_elements(query="Template")
    search = project.search("SecretMarker", file_type="metadata")

    assert overview["elements"]["total"] == 5
    assert templates["total"] == 0
    assert search["count"] == 0
    with pytest.raises(ProjectError, match="не является метаданными"):
        project.read_file("Sales/Resources/Template.yaml")


def test_related_docs_enriches_search_with_symbol_and_file_context(
    tmp_path: Path,
    element_project_path: Path,
    corpus_path: Path,
) -> None:
    semantic = semantic_service(tmp_path, element_project_path, corpus_path)

    result = semantic.related_docs(symbol="FindOrder", relative_path="Sales/Orders.xbsl", limit=2)

    assert result["status"] == "ready"
    assert "FindOrder" in result["derived_query"]
    assert "метод" in result["derived_query"]
    assert result["project_context"]["file"]["element"]["name"] == "Orders"
    assert result["documentation"]["count"] >= 1
    assert result["documentation"]["results"][0]["product_version"] == "9.2.4-6"
    assert result["next_step"].endswith("get_document.")


def test_semantic_tools_reject_invalid_symbol_and_path(
    tmp_path: Path,
    element_project_path: Path,
    corpus_path: Path,
) -> None:
    semantic = semantic_service(tmp_path, element_project_path, corpus_path)

    with pytest.raises(ValueError, match="одно корректное имя"):
        semantic.find_references("FindOrder()")
    with pytest.raises(ProjectError, match="за пределами активного проекта"):
        semantic.related_docs(relative_path="../secret.yaml")
    with pytest.raises(ValueError, match="Укажите symbol"):
        semantic.related_docs()
