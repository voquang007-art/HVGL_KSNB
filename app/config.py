# -*- coding: utf-8 -*-
from __future__ import annotations

import os
from pathlib import Path


class Settings:
    APP_NAME = os.environ.get("APP_NAME", "Cổng làm việc Ban Kiểm soát nội bộ")
    COMPANY_NAME = os.environ.get("COMPANY_NAME", "CÔNG TY TNHH BỆNH VIỆN HÙNG VƯƠNG GIA LAI")

    BASE_DIR = Path(__file__).resolve().parent
    ROOT_DIR = BASE_DIR.parent
    INSTANCE_DIR = ROOT_DIR / "instance"

    UPLOAD_DIR = os.environ.get(
        "UPLOAD_DIR",
        str(INSTANCE_DIR / "uploads"),
    )

    MAX_FILE_SIZE_MB = int(os.environ.get("MAX_FILE_SIZE_MB", "25"))


settings = Settings()