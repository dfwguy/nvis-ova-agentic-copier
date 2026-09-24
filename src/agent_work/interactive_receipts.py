# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Durable, non-authoritative specialist review receipts."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SpecialistReceipt:
    """One model-authored review result retained as workflow context."""

    role: str
    verdict: str
    detail: str


class InteractiveReceiptStore:
    """Persist specialist receipts and a small index for resumed workflows."""

    _RECEIPTS_FILE = "receipts.json"
    _MANIFEST_FILE = "manifest.json"

    def __init__(self, root: Path) -> None:
        self._root = root

    def save(self, receipts: tuple[SpecialistReceipt, ...]) -> None:
        """Atomically replace the receipt set and its compact manifest."""
        roles = tuple(receipt.role for receipt in receipts)
        if len(roles) != len(set(roles)):
            raise ValueError("duplicate role")
        payload = [
            {
                "detail": receipt.detail,
                "role": receipt.role,
                "verdict": receipt.verdict,
            }
            for receipt in receipts
        ]
        manifest = {"receipt_count": len(receipts), "roles": list(roles)}
        self._write_json(self._RECEIPTS_FILE, payload)
        self._write_json(self._MANIFEST_FILE, manifest)

    def load(self) -> tuple[SpecialistReceipt, ...]:
        """Load all retained receipts as review context."""
        path = self._root / self._RECEIPTS_FILE
        if not path.exists():
            return ()
        payload = json.loads(path.read_text(encoding="utf-8"))
        return tuple(SpecialistReceipt(**item) for item in payload)

    def manifest(self) -> dict[str, object]:
        """Return the compact receipt index."""
        path = self._root / self._MANIFEST_FILE
        if not path.exists():
            return {"receipt_count": 0, "roles": []}
        return json.loads(path.read_text(encoding="utf-8"))

    def _write_json(self, name: str, value: object) -> None:
        self._root.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor, temporary_name = tempfile.mkstemp(
            dir=self._root, prefix=".receipt-", text=True
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(value, handle, separators=(",", ":"), sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            temporary.chmod(0o600)
            temporary.replace(self._root / name)
        except OSError:
            temporary.unlink(missing_ok=True)
            raise
