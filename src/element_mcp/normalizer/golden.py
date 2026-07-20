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
    return mismatches
