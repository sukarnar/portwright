"""The workflow canvas: step order follows the connectors, the start follows the links, numbers on the cards."""
from portwright.studio import graph_vm as gv


def chain(*kinds):
    g = gv.Graph()
    return g, [gv.add_node(g, k) for k in kinds]


def test_steps_are_saved_in_the_order_the_links_run_not_the_order_they_were_added():
    g, (out, rep, start) = chain("output", "report", "input")     # added back to front
    gv.set_start(g, start.id)
    gv.connect(g, start.id, "next", rep.id)
    gv.connect(g, rep.id, "next", out.id)
    assert [s["id"] for s in gv.to_steps(g)] == [start.id, rep.id, out.id]
    assert gv.step_numbers(g) == {start.id: 1, rep.id: 2, out.id: 3}


def test_a_step_linked_in_front_of_the_start_becomes_the_start():
    g, (a, b, c) = chain("report", "email", "input")              # report was added first, so it was the start
    assert g.start == a.id
    gv.connect(g, a.id, "next", b.id)
    gv.connect(g, c.id, "next", a.id)                              # input -> report: input is now first
    assert g.start == c.id
    assert [s["id"] for s in gv.to_steps(g)] == [c.id, a.id, b.id]


def test_a_loop_back_to_the_start_does_not_move_it():
    g, (a, b, stray) = chain("input", "llm", "set")                 # 'stray' is not linked to anything
    gv.connect(g, a.id, "next", b.id)
    gv.connect(g, b.id, "next", a.id)                              # retry loop
    assert g.start == a.id
    assert gv.step_numbers(g) == {a.id: 1, b.id: 2}                # the stray step has no number
    assert gv.to_steps(g)[-1]["id"] == stray.id


def test_branches_stay_together():
    g, (start, cond, t1, t2, e1, e2) = chain("input", "condition", "set", "set", "set", "set")
    gv.connect(g, start.id, "next", cond.id)
    gv.connect(g, cond.id, "then", t1.id)
    gv.connect(g, cond.id, "else", e1.id)
    gv.connect(g, t1.id, "next", t2.id)
    gv.connect(g, e1.id, "next", e2.id)
    order = [s["id"] for s in gv.to_steps(g)]
    assert order == [start.id, cond.id, t1.id, t2.id, e1.id, e2.id]   # then-branch, then else-branch


def test_saving_does_not_change_what_runs():
    g, (start, cond, t1, e1, done) = chain("input", "condition", "set", "set", "output")
    for src, port, dst in [(start, "next", cond), (cond, "then", t1), (cond, "else", e1),
                           (t1, "next", done), (e1, "next", done)]:
        gv.connect(g, src.id, port, dst.id)
    steps = gv.to_steps(g)
    again = gv.to_steps(gv.from_steps(steps))
    assert [s["id"] for s in again] == [s["id"] for s in steps]
    assert {(s["id"], s.get("next"), s.get("then"), s.get("else")) for s in again} == \
           {(s["id"], s.get("next"), s.get("then"), s.get("else")) for s in steps}


def test_tidy_layout_reads_left_to_right_in_run_order():
    g, (c, b, a) = chain("output", "set", "input")
    gv.set_start(g, a.id)
    gv.connect(g, a.id, "next", b.id)
    gv.connect(g, b.id, "next", c.id)
    gv.auto_layout(g)
    assert a.x < b.x < c.x
