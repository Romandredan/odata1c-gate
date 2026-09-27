from tools.worktrees import (
    Копия,
    повторы_слоёв,
    разобрать_расхождение,
    разобрать_список,
    слой,
)

ВЫВОД = """worktree H:/repo
HEAD ce9838b0000000000000000000000000000000000
branch refs/heads/dev

worktree H:/repo/.claude/worktrees/gate-guard
HEAD 118efee0000000000000000000000000000000000
branch refs/heads/worktree-gate-guard
locked claude session 42

worktree H:/repo/.claude/worktrees/lost
HEAD 1111111000000000000000000000000000000000
detached
prunable gitdir file points to non-existent location

"""


def test_разбор_списка_рабочих_копий():
    копии = разобрать_список(ВЫВОД)

    assert копии == [
        Копия(путь="H:/repo", head="ce9838b0000000000000000000000000000000000", ветка="dev"),
        Копия(
            путь="H:/repo/.claude/worktrees/gate-guard",
            head="118efee0000000000000000000000000000000000",
            ветка="worktree-gate-guard",
            locked=True,
        ),
        Копия(
            путь="H:/repo/.claude/worktrees/lost",
            head="1111111000000000000000000000000000000000",
            prunable=True,
        ),
    ]


def test_разбор_bare_и_locked_без_причины():
    копии = разобрать_список("worktree /srv/repo.git\nbare\n\nworktree /w\nHEAD abc\nlocked\n")

    assert копии[0].bare is True
    assert копии[1].locked is True
    assert копии[1].ветка is None


def test_слой_по_первому_сегменту_имени():
    assert слой("worktree-gate-guard-word-boundaries") == "gate"
    assert слой("worktree-devtools-worktrees-overview") == "devtools"
    assert слой("write-table-parts") == "write"
    assert слой("worktree-base-url-publication-root") is None
    assert слой("dev") is None
    assert слой(None) is None


def test_повторы_слоёв_без_неизвестных():
    повторы = повторы_слоёв(
        [
            "worktree-gate-guard",
            "worktree-gate-tail",
            "worktree-write-lines",
            "worktree-base-url",
            "worktree-base-other",
        ]
    )

    assert повторы == {"gate": ["worktree-gate-guard", "worktree-gate-tail"]}


def test_расхождение_сначала_отставание_потом_опережение():
    assert разобрать_расхождение("1\t2") == (1, 2)
    assert разобрать_расхождение("") is None
    assert разобрать_расхождение("fatal: bad revision") is None
