import hashlib

from agent_memory.secrets_filter import LiteralSecrets, SecretFilter, luhn_ok

F = SecretFilter()

# Fake tokens are built in parts so no scanner mistakes them for real ones
FAKE_ANT = "sk-ant-" + "api03-" + "A1b2C3d4" * 6
FAKE_GH = "gh" + "p_" + "a1B2c3D4e5" * 4
FAKE_SLACK = "xo" + "xb-" + "1234567890-abcdefghijkl"
FAKE_AWS = "AK" + "IA" + "ABCDEFGHIJKLMNOP"
FAKE_TG = "123456789" + ":" + "AA" + "h" * 33
FAKE_OPENAI = "sk-" + "proj-" + "x9Y8z7W6v5" * 3


def red(t):
    return F.redact(t)[0]


def test_luhn():
    assert luhn_ok("4111111111111111")
    assert not luhn_ok("4111111111111112")


def test_card_redacted_with_separators():
    for card in ("4111 1111 1111 1111", "4111-1111-1111-1111", "4111111111111111", "5555555555554444",
                 "378282246310005", "3782 822463 10005"):
        out = red(f"paga con {card} hoy")
        assert "[REDACTED:card]" in out, card
        assert card not in out


def test_card_false_positives_kept():
    for s in ("4111111111111112",          # invalid Luhn
              "1727970000000",             # timestamp in ms
              "update_id 1234567890123",   # generic id
              "0f0f0f0f-1111-4222-8333-444444444444",
              "11111111-2222-3333-4444-555555555555"):  # UUID with a part that passes Luhn
        assert red(s) == s, s


def test_tokens():
    for tok, tag in ((FAKE_ANT, "anthropic"), (FAKE_GH, "github"), (FAKE_SLACK, "slack"), (FAKE_AWS, "aws"),
                     (FAKE_TG, "telegram-bot"), (FAKE_OPENAI, "openai")):
        out = red(f"el token es {tok} ok")
        assert tok not in out, tag
        assert "[REDACTED:" in out


def test_private_key():
    pk = "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAAA\nAAAA\n-----END OPENSSH PRIVATE KEY-----"
    out = red("clave:\n" + pk + "\nfin")
    assert "b3BlbnNzaC1rZXktdjEAAAAA" not in out
    assert out.endswith("fin")


def test_assignments():
    out = red('password=hunter2bis y "api_key": "abcd1234efgh" y DB_PASSWORD: s3cr3tvalue y contraseña: Mi-Clave-99')
    for s in ("hunter2bis", "abcd1234efgh", "s3cr3tvalue", "Mi-Clave-99"):
        assert s not in out
    assert "password=" in out  # the key is kept, only the value is redacted


def test_assignments_placeholders_and_prose_kept():
    for s in ("password=${DB_PASS}", "token: null", "mapping: valores", "el campo password", "max_tokens=1000"):
        assert red(s) == s, s


def test_url_credentials():
    out = red("conecta a postgres://bob:SuperPass123@db.local:5432/x")
    assert "SuperPass123" not in out and "postgres://bob:" in out


def test_literal_secret_by_hash():
    secret = "Zx9" + "q" * 58 + "W1/"
    lit = LiteralSecrets([(len(secret), hashlib.sha256(secret.encode()).hexdigest())])
    f = SecretFilter(lit)
    text = f"la contraseña es «{secret}». Y otra vez:{secret}"
    out, n = f.redact(text)
    assert secret not in out and n >= 2
    assert out.startswith("la contraseña es «[REDACTED:secret]»")


def test_literal_file(tmp_path):
    secret = "abc" * 22
    p = tmp_path / "l.sha256"
    p.write_text(f"# comment\n{hashlib.sha256(secret.encode()).hexdigest()} {len(secret)}\n")
    f = SecretFilter(LiteralSecrets.from_file(str(p)))
    assert secret not in f.redact("x " + secret + " y")[0]


def test_prose_passwords():
    cases = ["la contraseña es Gato-Azul-77 ok", "my password is Tr0ub4dor&3.", "la clave del wifi es 'perro2024'",
             "Password: SuperSecreta99", "el pin es 4821"]
    for s, secret in zip(cases, ["Gato-Azul-77", "Tr0ub4dor&3", "perro2024", "SuperSecreta99", "4821"]):
        out = red(s)
        assert secret not in out and "[REDACTED:secret]" in out, s
    assert red("my password is Tr0ub4dor&3.").endswith("[REDACTED:secret].")


def test_prose_false_positives_kept():
    for s in ("la contraseña es incorrecta", "the password is wrong", "la clave es muy larga"):
        assert red(s) == s, s


def test_basic_auth():
    out = red("Authorization: Basic Ym9iOnRvcHNlY3JldHZhbHVl")
    assert "Ym9iOnRvcHNlY3JldHZhbHVl" not in out


def test_card_with_dots():
    out = red("tarjeta 4111.1111.1111.1111 caduca")
    assert "4111.1111" not in out and "[REDACTED:card]" in out
    # versions, IPs and dotted numbers that fail Luhn are not cards
    for s in ("v1.2.3", "192.168.10.20", "1234.5678.9012.3456"):
        assert red(s) == s, s


def test_plain_text_untouched():
    s = "Hoy configuramos borg con el NAS y revisamos el MCP de la memoria. Tomorrow, the chat bot."
    assert red(s) == s


# ---------------------------------------------------------------- rules brought over from openclaw.py
def test_password_value_with_comma():
    for t in ("Contraseña: Ab12,cd#_Rn", "contraseña: Ab12,cd#_Rn", "password: Ab12,cd#_Rn", "pass: Ab12,cd#_Rn",
              "Contraseña=Ab12,cd#_Rn"):
        out, counts = F.redact_detail(t)
        assert "Ab12" not in out and "cd#_Rn" not in out, t
        assert out.endswith("[REDACTED:secret]"), t
    out = red("Contraseña: Ab12,cd#_Rn, y el usuario es bob")
    assert out == "Contraseña: [REDACTED:secret], y el usuario es bob"


def test_password_word_without_value_kept():
    for s in ("Contraseña:", "la contraseña, por favor", "Olvidé la contraseña.", "Cambia la password cuando puedas",
              "contraseña incorrecta, prueba otra vez"):
        assert red(s) == s, s


def test_tail_keeps_normal_punctuation():
    assert red("la contraseña es Zq81xx. Luego entra") == "la contraseña es [REDACTED:secret]. Luego entra"
    assert red('{"password":"abc123","user":"bob"}') == '{"password":[REDACTED:secret],"user":"bob"}'
    # The glued tail is not eaten after other labels (URL credentials keep their host)
    assert red("postgres://bob:SuperPass123@db.local/x") == "postgres://bob:[REDACTED:password]@db.local/x"


def test_backtick_env_keys():
    for t in ("- `MCP_PASSWORD`: Sup3rClave", "**`DB_TOKEN`**: Sup3rClave", "`STRIPE_KEY`: Sup3rClave",
              "OPENROUTER_API_KEY=Sup3rClave", "MY_SECRET: Sup3rClave", "SERVICE_KEY=Sup3rClave",
              "`client_secret`: Sup3rClave", "dbPassword=Sup3rClave"):
        out = red(t)
        assert "Sup3rClave" not in out and "[REDACTED:secret]" in out, t


def test_env_keys_false_positives_kept():
    for s in ("key: value", "primary key: id", "`max_tokens`: 1000", "maxTokens: 4096", "monkey: banana",
              "`MCP_PASSWORD`: ${MCP_PASSWORD}", "API_KEY=<clave>", "Usa la variable `DB_PASSWORD` del .env"):
        assert red(s) == s, s


def test_oauth_url_params():
    url = "https://accounts.google.com/o/oauth2/callback?state=Xy12abcdefgh&code=4/0AeoWuMxyz123&scope=email"
    out, counts = F.redact_detail(url)
    assert "Xy12abcdefgh" not in out and "0AeoWuMxyz123" not in out
    assert "&scope=email" in out and "?state=[REDACTED:url]" in out
    assert counts["url-param"] == 2


def test_url_and_code_false_positives_kept():
    for s in ("Revisa el code de la función", "code = compile(src)", "el state=ok de la API",
              "https://github.com/x/y/issues?state=closed&page=2", "https://example.com/docs?lang=es",
              "mira https://www.youtube.com/watch?v=dQw4w9WgXcQ"):
        assert red(s) == s, s


def test_cookie_lines():
    dump = ".youtube.com\tTRUE\t/\tTRUE\t1811091227\tSID\tAbCdEf123456\n.x.com TRUE / FALSE 1 A b2c3d4"
    out, counts = F.redact_detail(dump)
    assert "AbCdEf123456" not in out and "b2c3d4" not in out and counts["cookie"] == 2
    assert out.startswith(".youtube.com\tTRUE\t/\tTRUE\t1811091227\tSID\t[REDACTED:cookie]")
    s = "Si ves algo así:\n.youtube.com\tTRUE\t/\tTRUE\t...\nsí me sirve"
    assert red(s) == s


def test_idempotent_and_detail_counts():
    t = ("- `MCP_PASSWORD`: Sup3rClave\nContraseña: Ab12,cd#_Rn\n?code=4/0AeoWuMxyz123 tarjeta 4111 1111 1111 1111 "
         "y " + FAKE_GH)
    once, counts = F.redact_detail(t)
    assert sum(counts.values()) == F.redact(t)[1]
    assert {"assignment", "url-param", "card", "github"} <= set(counts)
    twice, again = F.redact_detail(once)
    assert twice == once and not again


def test_marker_is_recognized():
    from agent_memory.secrets_filter import count_markers
    done = "la contraseña es [REDACTED:secret] y password=[REDACTED:secret]"
    assert red(done) == done
    assert count_markers(done + " [REDACTED:github]") == 3
