from __future__ import annotations

from app.services.click.bot_detection import get_bot_name, is_bot_request

GOOGLEBOT_UA = (
    "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"
)
CHROME_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36"


class TestIsBotRequest:
    def test_googlebot_detected(self):
        assert is_bot_request(GOOGLEBOT_UA) is True

    def test_normal_browser_not_bot(self):
        assert is_bot_request(CHROME_UA) is False

    def test_custom_pattern_match(self, mocker):
        mocker.patch(
            "app.services.click.bot_detection._load_bot_user_agents",
            return_value=["TestBot/1\\.0"],
        )
        mocker.patch(
            "app.services.click.bot_detection._crawler_detect"
        ).isCrawler.return_value = False
        assert is_bot_request("TestBot/1.0 (test)") is True

    def test_falls_back_to_crawler_detect_when_no_patterns(self, mocker):
        mocker.patch(
            "app.services.click.bot_detection._load_bot_user_agents", return_value=[]
        )
        assert is_bot_request(GOOGLEBOT_UA) is True


class TestGetBotName:
    def test_non_bot_returns_none(self):
        assert get_bot_name(CHROME_UA) is None

    def test_googlebot_returns_string(self):
        name = get_bot_name(GOOGLEBOT_UA)
        assert name is not None and isinstance(name, str)

    def test_pattern_match_returns_pattern(self, mocker):
        mocker.patch(
            "app.services.click.bot_detection._load_bot_user_agents",
            return_value=["MyCustomBot"],
        )
        mock_cd = mocker.patch("app.services.click.bot_detection._crawler_detect")
        mock_cd.isCrawler.return_value = False
        mock_cd.getMatches.return_value = None
        assert get_bot_name("MyCustomBot/2.0") == "MyCustomBot"
