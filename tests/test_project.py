from __future__ import annotations

from pathlib import Path

import pytest

from element_mcp.config import ServerSettings
from element_mcp.project import ProjectError, ProjectService


def service(tmp_path: Path, project_path: Path | None = None) -> ProjectService:
    return ProjectService(
        ServerSettings(
            project_path=project_path,
            config_path=tmp_path / "config.json",
            data_path=tmp_path / "data",
        )
    )


def test_project_status_is_missing_before_connection(tmp_path: Path) -> None:
    assert service(tmp_path).project_status() == {
        "status": "missing",
        "message": "Проект 1С:Предприятие.Элемент не подключён",
        "path": None,
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
