"""Shared welcome template with literal, single-pass substitutions."""

from dataclasses import dataclass
import re

DEFAULT_WELCOME = (
    'Привет! Добро пожаловать в {server_name}.\n\n'
    'Я — серверный помощник. В этот чат будут приходить новости и объявления сервера.\n\n'
    'Чтобы пригласить друга, открой реферального бота и отправь команду «пригласить». '
    'Он выдаст код для регистрации.\n'
    '{referral_url}\n\n'
    'Я на сообщения не отвечаю. Если нужна помощь, напиши администратору:\n'
    '{administrator_url}'
)


def matrix_url(mxid: str, domain: str) -> str:
    if not isinstance(mxid, str) or not re.fullmatch(
        r'@[a-z0-9][a-z0-9._=-]{0,63}:' + re.escape(domain), mxid
    ):
        raise RuntimeError('Invalid local welcome identity')
    return 'https://matrix.to/#/' + mxid


@dataclass(frozen=True)
class WelcomeContext:
    domain: str
    server_name: str
    referral_mxid: str
    administrator_mxid: str

    def render(self, template: str) -> str:
        values = {
            'domain': self.domain,
            'server_name': self.server_name,
        }
        if '{referral_url}' in template:
            values['referral_url'] = matrix_url(self.referral_mxid, self.domain)
        if '{administrator_url}' in template:
            values['administrator_url'] = matrix_url(self.administrator_mxid, self.domain)
        # Custom text may contain JSON, other braces or placeholders in a name.
        # Only supported placeholders in the original template are replaced.
        return re.sub(
            r'\{(domain|server_name|referral_url|administrator_url)\}',
            lambda match: values[match[1]],
            template,
        )
