import unittest
from unittest.mock import patch, MagicMock
from app.agents.engine import _build_llm

class TestEngineCascade(unittest.TestCase):
    """[P0] Unit tests for LLM build fallback cascade"""

    @patch('app.agents.engine.ChatOpenAI')
    @patch('app.agents.engine.ChatGroq')
    @patch('app.agents.engine.settings')
    def test_build_llm_contains_cascade_models(self, mock_settings, mock_groq, mock_openai):
        mock_settings.gemini_api_key = "test-gemini-key"
        mock_settings.nvidia_api_key = "test-nvidia-key"
        mock_settings.groq_api_key = "test-groq-key"
        mock_settings.openrouter_api_key = ""

        llm = _build_llm()
        self.assertIsNotNone(llm)
