"""计数加速必须保持已有字符分类、混合文本取整和预算边界。"""

from local_agent.context.token_budget import estimate_tokens, fits_budget, is_wide_char


def test_estimate_tokens_preserves_every_unicode_codepoint() -> None:
    # 四个相同字符可以区分宽字符的 4 token 与窄字符的 1 token；单字符两者都是 1。
    for code in range(0x110000):
        character = chr(code)
        assert estimate_tokens(character * 4) == (4 if is_wide_char(character) else 1), hex(code)


def test_mixed_text_rounding_preserves_budget_boundaries() -> None:
    texts = [
        "", "a", "abc", "abcd", "abcde", "中文abcde", "e\u0301中文",
        "\x00\r\n\t全角，。", "\ud800x\udfff", "👩\u200d💻汉字\U00020000",
        '{"path":"欢迎页.tsx","old_text":"<main>\\r\\n","new_text":"中文abc"}',
        ("const title = '开始';\r\n" * 1000) + "尾部",
    ]
    for text in texts:
        wide = sum(map(is_wide_char, text))
        expected = wide + (len(text) - wide + 3) // 4
        assert estimate_tokens(text) == expected
        assert fits_budget(text, expected)
        assert not fits_budget(text, expected - 1)
