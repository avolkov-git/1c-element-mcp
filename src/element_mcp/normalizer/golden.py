from __future__ import annotations

from typing import Any

GOLDEN_CORPORA: dict[str, dict[str, dict[str, Any]]] = {
    "9.2.4-6": {
        "lang": {
            "documents": 2476,
            "chunks": 32273,
            "documents_sha256": "9057696f2cc0456a8ab458607a419efffb99176c7cb4da3ac5ecf9a4e130a0ac",
            "vector_input_sha256": "353354a32fc7abaee4ec854a8d481788aac2d9f1773da864305e7b9fa9181009",
            "artifact_sha256": {
                "documents.jsonl": "c8e090d2ad28c6664dba1b81cca7d93e1bf3bb2da559e6c03233ebfe8fce266f",
                "chunks.jsonl": "fc27b68db7c257e2fd46bd1e8cbfe14df28e6b78a7ee78ca5a367721533aaf20",
                "vectors.f16.npy": "7ac3f649ceeb98cf885634b75e189b7b14f34b6e9b2aee2dd573d82afa0cad00",
                "vector-idf.npy": "e428539696dca06a871657e9df4a3d25f54630a1cf89a600d56d62db8d20d187",
                "vector-ids.jsonl": "f2587195d0ce106f9e60b75b13aaf61ba53c5e615f3a9648beea71e03ae6b63b",
            },
        },
        "console": {
            "documents": 4397,
            "chunks": 19891,
            "documents_sha256": "c0d36fa28c8e17874f45ba889c11f9788aab58ed88ad6fcb34202b56b08980f8",
            "vector_input_sha256": "e7f893510c2819dda5b984c53df1acf6aa0844781e684ac06cff81bdf9a2cb83",
            "artifact_sha256": {
                "documents.jsonl": "f69b33fd1ef951e345997f701f48607779c38e4daeba10015bd40742121f7e7d",
                "chunks.jsonl": "834abcc65dd856499ea1e4db1edf6d25928288b5606b2c8ce9b2f0973a4d7e20",
                "vectors.f16.npy": "127dcf9f90e5b661cd8d4f944927b6d73620027cf971549b7ccd172b4d3124a7",
                "vector-idf.npy": "198492d5568f0812cf553f7a6e99fd33b231e51354a0b2ad9e37f107f4699a2f",
                "vector-ids.jsonl": "df2233e9515af5e7dc06d7fd5672bcd5844dded3c14e0044e60bf8413fff96f1",
            },
        },
        "server": {
            "documents": 3052,
            "chunks": 7358,
            "documents_sha256": "ea6e848f0efedd6289c48729d57ffadd42a2f30a359196655e6a305e11258584",
            "vector_input_sha256": "a24a386176e7ec0504d1a8e7dd22998bc7c9cf781a263ec65cc48e91d7c8ba53",
            "artifact_sha256": {
                "documents.jsonl": "e83bff222d75680306d61e69a8b34688b19e300d6b6d59a89a708761bf4275fb",
                "chunks.jsonl": "ea106db686d892a78c8a81a3c6f2584fe9fd9ca394566aeeef24640894a50ff2",
                "vectors.f16.npy": "c61985ab5b5a801e769d0a183c92a3cc0427f448c595ce624ffe8bada55df4d7",
                "vector-idf.npy": "9c27b2e498afb73a24b7ef1dd9eefeeb9c7758557e3220ab687dde8893e8154a",
                "vector-ids.jsonl": "7e05e3a630a7af5a29ffa986a4f90374f2ae56af40ab8f3234fcbbc1264ee5e3",
            },
        },
    }
}

GOLDEN_REFERENCES: dict[str, dict[str, tuple[int | None, str]]] = {
    "9.2.4-6": {
        "lang.9.2.4-6.lang-link-graph": (
            None,
            "1dcc7ef581abec160e310ffc9ee99604d3cc1552fd76f5760c4d0e94c7028f09",
        ),
        "console.9.2.4-6.api-operations": (
            166,
            "4fed50641cb1b4fa2c338c1a02e7df202e79dced7a5fce2d29cd320a6511d88c",
        ),
        "console.9.2.4-6.api-schemas": (
            87,
            "b375ff76263c748d925dbccb93f1b5c5a75b0720f3a804a004ebcc3129f13800",
        ),
        "console.9.2.4-6.elements": (
            2246,
            "714f089d6dea7c3123bcb163e96137ae13a32ed03bcdbeae8b230ad524151e75",
        ),
        "console.9.2.4-6.http-routes": (
            466,
            "4261a67c3a2bc59d12ad95073c0e9026694b47925961877e3bbbea129229fd44",
        ),
        "console.9.2.4-6.imports": (
            3472,
            "3a424ad538f8876ddb36926b4021fb765744e2df6156524d0b634c7782e91149",
        ),
        "console.9.2.4-6.official-link-graph": (
            None,
            "681e942f6bb9b75a6a12b18763fdebd7448bbac1e19926acb191b914f25908c4",
        ),
        "console.9.2.4-6.methods": (
            13558,
            "557af417a1bc50c04eff6d6f121de7575f7d3e1c69d7704d9f2269aafc382dc9",
        ),
        "console.9.2.4-6.subsystems": (
            48,
            "cab2050fb1bbf02dae72f8a5dff1663c3041e5928443db75eaaca2431a433f05",
        ),
        "server.9.2.4-6.files": (
            19285,
            "a3b55f90c92fdb1755df17a6aa9571f66c665610348a36f0e29555fe66338c0f",
        ),
        "server.9.2.4-6.jar-packages": (
            16127,
            "73377d70994aa5c5f0c37a2823450a448a28d149c2af8bce5def1537a50973be",
        ),
        "server.9.2.4-6.jars": (
            1496,
            "271c6809b1b0809b2b0a9569c5ded3f91ac4f3f20db06fa4188a3af5e00a56d6",
        ),
        "server.9.2.4-6.components": (
            10,
            "5abc0a5552063f5311fe2312daf08e3761522a404fefa6cc8efe208d42633999",
        ),
        "server.9.2.4-6.config-files": (
            14,
            "cc8ea0be5da03f3c10ecea2d252bdb17d511e4206dbaada655753885ae6b797f",
        ),
        "server.9.2.4-6.connections": (
            4,
            "541b5a21b591cb7f5789792b00353f5bff642edd31f4d3656685f00e3e689ce8",
        ),
        "server.9.2.4-6.entrypoints": (
            6,
            "b295eef7f6e814ab57ee57ea0b043dd8545f142f22d6f61299d82281540244c5",
        ),
        "server.9.2.4-6.extensions": (
            16,
            "ad1bbd49cdaa2b933ef7eae7897cff4d24de37628f0d79463c2109e3a8ea94c9",
        ),
        "server.9.2.4-6.host-modules": (
            80,
            "f31359100c21aec3ef8dcd428195f461b2ebfd6f7e0da40bffb5eea711b042a5",
        ),
        "server.9.2.4-6.server-link-graph": (
            None,
            "32237343ad17130129774f56f7d98eaac4710dbdcdc4f6fed1e180ad61b3b5db",
        ),
    }
}


def golden_mismatches(report: dict[str, Any], product_version: str) -> list[str]:
    expected = GOLDEN_CORPORA.get(product_version)
    if expected is None:
        return []
    actual = {item["corpus"]: item for item in report.get("corpora", [])}
    mismatches: list[str] = []
    for corpus_name, expected_values in expected.items():
        item = actual.get(corpus_name)
        if item is None:
            mismatches.append(f"{corpus_name}: отсутствует отчёт")
            continue
        for field, expected_value in expected_values.items():
            if item.get(field) != expected_value:
                mismatches.append(f"{corpus_name}.{field}: получено {item.get(field)!r}, ожидалось {expected_value!r}")
    expected_references = GOLDEN_REFERENCES.get(product_version, {})
    actual_references = {
        row["id"]: (row.get("records"), row.get("sha256"))
        for row in report.get("references", {}).get("dataset_checks", [])
    }
    if set(actual_references) != set(expected_references):
        missing = sorted(set(expected_references) - set(actual_references))
        extra = sorted(set(actual_references) - set(expected_references))
        mismatches.append(f"references.datasets: отсутствуют {missing}, лишние {extra}")
    for dataset_id, expected_value in expected_references.items():
        if dataset_id in actual_references and actual_references[dataset_id] != expected_value:
            mismatches.append(
                f"references.{dataset_id}: получено {actual_references[dataset_id]!r}, ожидалось {expected_value!r}"
            )
    return mismatches
