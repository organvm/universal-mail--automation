import pytest
from unittest.mock import patch

from cli import get_provider, main

def test_get_provider_gmail():
    with patch('providers.gmail.GmailProvider') as MockProvider:
        provider = get_provider('gmail')
        MockProvider.assert_called_once()
        assert provider == MockProvider.return_value

def test_get_provider_imap():
    with patch('providers.imap.IMAPProvider') as MockProvider:
        provider = get_provider('imap', host='imap.example.com', user='testuser', password='password', use_gmail_extensions=True)
        MockProvider.assert_called_once_with(host='imap.example.com', user='testuser', password='password', use_gmail_extensions=True)
        assert provider == MockProvider.return_value

def test_get_provider_mailapp():
    with patch('providers.mailapp.MailAppProvider') as MockProvider:
        provider = get_provider('mailapp', account='MyAccount')
        MockProvider.assert_called_once_with(account='MyAccount')
        assert provider == MockProvider.return_value

def test_get_provider_outlook():
    with patch('providers.outlook.OutlookProvider') as MockProvider:
        provider = get_provider('outlook')
        MockProvider.assert_called_once()
        assert provider == MockProvider.return_value

def test_get_provider_unknown():
    with pytest.raises(ValueError, match="Unknown provider: unknown"):
        get_provider('unknown')

@patch('cli.cmd_label')
def test_cli_main_label_command(mock_cmd_label, monkeypatch):
    mock_cmd_label.return_value = 0
    monkeypatch.setattr('sys.argv', ['cli.py', 'label', '--provider', 'gmail', '--query', 'test query'])
    assert main() == 0
    mock_cmd_label.assert_called_once()
    args = mock_cmd_label.call_args[0][0]
    assert args.provider == 'gmail'
    assert args.query == 'test query'

@patch('cli.cmd_report')
def test_cli_main_report_command(mock_cmd_report, monkeypatch):
    mock_cmd_report.return_value = 0
    monkeypatch.setattr('sys.argv', ['cli.py', 'report', '--provider', 'outlook'])
    assert main() == 0
    mock_cmd_report.assert_called_once()
    args = mock_cmd_report.call_args[0][0]
    assert args.provider == 'outlook'

@patch('cli.cmd_health')
def test_cli_main_health_command(mock_cmd_health, monkeypatch):
    mock_cmd_health.return_value = 0
    monkeypatch.setattr('sys.argv', ['cli.py', 'health', '--provider', 'imap'])
    assert main() == 0
    mock_cmd_health.assert_called_once()

def test_cli_no_args(capsys, monkeypatch):
    monkeypatch.setattr('sys.argv', ['cli.py'])
    assert main() == 1
    captured = capsys.readouterr()
    assert "usage:" in captured.out
