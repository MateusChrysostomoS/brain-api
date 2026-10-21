"""Test double for services/mailer.send_email — installed for EVERY test by conftest.

No test may open a real SMTP connection. A test that wants a refused send sets
`mail_spy.result = False`; one that inspects what went out reads `mail_spy.calls`.
"""


class MailSpy:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []
        self.result = True

    async def __call__(self, to: str, template: str, variables) -> bool:
        self.calls.append((to, template, dict(variables)))
        return self.result

    def of(self, template: str) -> list[tuple[str, str, dict]]:
        return [call for call in self.calls if call[1] == template]

    @property
    def last(self) -> tuple[str, str, dict]:
        assert self.calls, "expected mailer.send_email to have been called"
        return self.calls[-1]
