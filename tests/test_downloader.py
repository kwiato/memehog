from memehog.core.downloader import classify_url
from memehog.search.fts import build_match_query


def test_classify_instagram():
    assert classify_url("https://www.instagram.com/reel/xyz/") == "instagram"
    assert classify_url("https://instagram.com/p/abc/") == "instagram"


def test_classify_tiktok():
    assert classify_url("https://www.tiktok.com/@user/video/123") == "tiktok"
    assert classify_url("https://vm.tiktok.com/ZM123/") == "tiktok"


def test_classify_direct():
    assert classify_url("https://example.com/funny.jpg") == "direct"
    assert classify_url("https://cdn.example.com/a/b/meme.mp4?sig=1") == "direct"


def test_classify_generic():
    assert classify_url("https://example.com/some-page") == "generic"


def test_build_match_query_quotes_tokens():
    assert build_match_query("kot w kapeluszu") == '"kot"* "w"* "kapeluszu"*'


def test_build_match_query_strips_fts_operators():
    # Quotes/parens/operators from user input must not break FTS5 syntax
    assert build_match_query('a "b OR c) NOT') == '"a"* "b"* "OR"* "c"* "NOT"*'


def test_build_match_query_empty():
    assert build_match_query("  !!!  ") == ""
