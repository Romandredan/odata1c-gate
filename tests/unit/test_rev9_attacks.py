"""Воспроизведения ревью задачи 9 плана M2 (ревьюер-атакующий, `ece5237`) — после Ruling 59.

Демон в памяти — путь прямого клиента без лаунчера: клиент называет себя в `initialize`, как это
сделал бы любой локальный процесс (в т. ч. `curl` из Bash модели). `test_А1` и `test_Д1` были
воспроизведениями находок и утверждали запись без подтверждения и эхо имени тула; здесь они в
обратной форме — находки закрыты (Ruling 59 и Н9-2). `test_А2` — принятая граница: против клиента,
который сам отвечает «yes» на elicitation, механизм по возможностям клиента не защищает.
"""

import json

import pytest
import test_daemon_write as д
import test_write_commit as к

from odata1c.daemon import SessionMechanisms
from odata1c.write.confirm import choose_mechanism

КОНТРАГЕНТЫ = к.КОНТРАГЕНТЫ
ССЫЛКА = к.ССЫЛКА
НОВЫЙ_ИНН = к.НОВЫЙ_ИНН
ПУТЬ_КОНТРАГЕНТА = к.ПУТЬ_КОНТРАГЕНТА

дом = д.дом  # noqa: F811
одинс = д.одинс  # noqa: F811
демон = д.демон  # noqa: F811


# -- А. Запись без подтверждения через самообъявление клиента --------------------------------


async def test_А1_самообъявление_claude_code_не_даёт_записи_без_подтверждения(демон, одинс):
    """Клиент называет себя `claude-code` 2.1.267 и НЕ объявляет elicitation (у curl её нет).
    До Ruling 59 демон выбирал механизм `claude_code` и выполнял запись без единого
    подтверждения. Теперь имя без подписи лаунчера не заверено: механизм — запасной `deny`,
    отказ, в 1С ни одного PATCH."""
    async with д.клиент(демон, имя="claude-code", версия="2.1.267", ответ=None) as кл:
        подготовка = await д.подготовить(кл, демон, одинс)
        текст = await кл.вызвать("odata1c_commit", {"pending_id": подготовка["pending_id"]})

    отказ = д.ошибка(текст)
    assert отказ["code"] == "write_unsupported_client"
    assert кл.вопросы == []
    assert одинс.patch.call_count == 0


async def test_А2_самообъявление_с_elicitation_скрипт_отвечает_yes_сам(демон, одинс):
    """Принятая граница (SPEC §7.2, Ruling 59): клиент объявляет elicitation и на вопрос сервера
    отвечает «yes» сам. Демон спрашивает — и получает «yes» от кода. Против самоотвечающего
    клиента не защищает ни один механизм по возможностям клиента: «yes» должен нажимать человек,
    а проверить это может только аутентификация клиента (M4)."""
    async with д.клиент(демон, имя="злой-скрипт", версия="1.0.0", ответ=д.ДА) as кл:
        подготовка = await д.подготовить(кл, демон, одинс)
        текст = await кл.вызвать("odata1c_commit", {"pending_id": подготовка["pending_id"]})

    assert "commit_id" in json.loads(текст)
    assert len(кл.вопросы) == 1  # вопрос был задан, но ответил на него код
    assert одинс.patch.call_count == 1


def test_А3_механизм_закреплён_первым_вызовом_смена_клиента_не_меняет(демон):
    """`SessionMechanisms` фиксирует механизм первым вызовом сессии: клиент, начавший как
    elicitation, не станет `claude_code`, назвавшись позже иначе (в протоколе 2026-07-28 имя
    приходит в каждом запросе) — даже с подписью."""
    механизмы = SessionMechanisms("deny")
    elic = д.ClientIdentity(name="иной", version="1.0.0", elicitation=True)
    cc = д.ClientIdentity(name="claude-code", version="2.1.267", elicitation=False, verified=True)
    assert механизмы.choose("s1", elic) == "elicitation"
    assert механизмы.choose("s1", cc) == "elicitation"  # не сменился
    # Обратный порядок — своя сессия: механизм сессии остаётся claude_code, но действует только на
    # подписанном запросе; неподписанный в той же сессии получает отказ (Ruling 59).
    assert механизмы.choose("s2", cc) == "claude_code"
    assert механизмы.choose("s2", elic) == "deny"
    assert механизмы.choose("s2", cc) == "claude_code"


def test_А4_trust_только_при_trust_client():
    без_elic = ("иной", "1.0.0", False)
    assert choose_mechanism(*без_elic, "deny") == "deny"
    assert choose_mechanism(*без_elic, "trust_client") == "trust"
    # Чистая функция выбора по имени; заверено ли имя подписью лаунчера — решает слой демона
    # (`SessionMechanisms`, Ruling 59).
    assert choose_mechanism("claude-code", "2.1.267", False, "deny") == "claude_code"


# -- Б. Сессии ------------------------------------------------------------------------------


async def test_Б1_commit_чужой_сессии_pending_unknown(демон, одинс):
    """Две сессии в памяти — разные объекты `initialize`, разные ключи. `commit` операции чужой
    сессии → `pending_unknown` без `pending_id` в тексте, PATCH нет."""
    async with д.клиент(демон, ответ=д.ДА) as первый, д.клиент(демон, ответ=д.ДА) as второй:
        подготовка = await д.подготовить(первый, демон, одинс)
        чужой = await второй.вызвать("odata1c_commit", {"pending_id": подготовка["pending_id"]})
        отказ = д.ошибка(чужой)
        assert отказ["code"] == "pending_unknown"
        assert подготовка["pending_id"] not in чужой
        assert одинс.patch.call_count == 0
        # Своя сессия ту же операцию выполняет.
        свой = await первый.вызвать("odata1c_commit", {"pending_id": подготовка["pending_id"]})
        assert "commit_id" in json.loads(свой)
        assert одинс.patch.call_count == 1


# -- В. Инвариант 1: отказ SDK на аргумент не того типа не повторяет ввод ---------------------


@pytest.mark.parametrize(
    ("тул", "аргументы"),
    [
        ("odata1c_commit", {"pending_id": {"ИНН": "7707083893"}}),
        ("odata1c_undo", {"commit_id": ["7707083893"]}),
        ("odata1c_update", {"entity": КОНТРАГЕНТЫ, "key": ССЫЛКА, "data": "7707083893"}),
        ("odata1c_journal", {"limit": "7707083893"}),
        ("odata1c_query", {"entity": {"x": "7707083893"}}),
    ],
)
async def test_В1_отказ_SDK_по_аргументу_без_эха(демон, тул, аргументы):
    async with д.клиент(демон) as кл:
        текст = await кл.вызвать(тул, аргументы)
    отказ = д.ошибка(текст)
    assert отказ["code"] == "params_invalid"
    assert "7707083893" not in текст  # ввод модели не повторён
    assert "input_value" not in текст


# -- Г. list_tools: описания не несут данных 1С ----------------------------------------------


async def test_Г1_list_tools_без_данных(демон):
    async with д.клиент(демон) as кл:
        тулы = (await кл.сессия.list_tools()).tools
    сериализовано = json.dumps([т.model_dump() for т in тулы], ensure_ascii=False, default=str)
    # Описания — статические, но проверим «от класса данных»: ни одного реального значения.
    к.нет_реальных_значений(сериализовано)


# -- Д. Прочие каналы ошибок SDK -------------------------------------------------------------


async def test_Д1_незнакомый_тул_с_ИНН_в_имени_отказ_без_имени(демон):
    """Вызов несуществующего тула, в имя которого вписан ИНН. SDK отвечал `ToolError` «Unknown
    tool: <имя>», отражая имя дословно, мимо гейта (Н9-2). Теперь `_GateServer` отвечает
    `params_invalid` §5.2 через стража, без имени."""
    async with д.клиент(демон) as кл:
        результат = await кл.сессия.call_tool("odata1c_7707083893", {})
    текст = результат.content[0].text if результат.content else ""
    assert "7707083893" not in текст
    assert д.ошибка(текст)["code"] == "params_invalid"
    assert результат.is_error


async def test_Д2_read_resource_с_ИНН_в_URI(демон):
    """Чтение ресурса по URI с ИНН в имени базы. Путь ошибки ресурса `_GateServer.call_tool` не
    трогает (это не тул). ИНН здесь — ВВОД модели (она сама собрала URI), и в ответе он
    возвращается только в отражённом поле `uri`; тело ошибки (`base_unknown`) реального значения
    не несёт. То же, что эхо `pending_id` модели: не утечка защищаемого класса, но фиксируем."""
    async with д.клиент(демон) as кл:
        try:
            результат = await кл.сессия.read_resource("odata1c://policy/7707083893")
            тела = [c.text for c in результат.contents if hasattr(c, "text")]
        except Exception as e:  # noqa: BLE001
            тела = [f"{type(e).__name__}: {e}"]
    # Тело ответа реального значения не несёт; ИНН только в отражённом URI (ввод модели).
    for тело in тела:
        assert "7707083893" not in тело
