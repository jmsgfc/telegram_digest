from __future__ import annotations

import re


def normalize_phone(country_code: str, phone_number: str) -> str:
    country_digits = re.sub(r"[\s+()-]", "", country_code)
    number_digits = re.sub(r"[\s()-]", "", phone_number)
    if not country_digits.isdigit():
        raise ValueError("区号格式不正确，例如中国大陆填写 +86。")
    if not number_digits.isdigit():
        raise ValueError("手机号只能包含数字。")
    if not number_digits:
        raise ValueError("请填写手机号。")
    return f"+{country_digits}{number_digits}"
