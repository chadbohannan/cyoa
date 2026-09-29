# Act pacing interview — 2026-09-29

Owner's request: "i would like to rework the Agents view. currently it provides limited insight into agent runtime state and a set of prompt editors. I would like to transform this view to drive pacing controls as a first class feature to mechanically pace the generation of stories through a 3 Act structure. the visualization should be of the agentic flow, selecting a node in the view selects the appropriate editor(s) on the sidebar."

Answers, as given:

- Act boundaries: depth-based (levels of the storyline), not beat milestones or the story clock.
- Per-act levers: branching per act, prompt per act, closure rules per act. (Not: required beats.)
- Persistence: per story, copied into each run.
- Diagram: acts as stages — `[Premise] → (Act I) → (Act II) → (Act III) → [Endings]`, each act expanding to its grow/gather steps.
- Per-act prompts: one set of frontier roles with an `{act_guidance}` slot, not a full role per act.
- Act node sidebar: act span + branching, closure toggles, act guidance text, underlying prompts.
- Turning points: not nodes; boundaries are numbers on the act nodes.
- Runs: config only — no live run overlay or launching in the Agents view; runtime state stays in the RunDock.
- Other workflows: fold review/refine into the pipeline; verbs and canon_check go to a separate section.
- Premise node: depth + steer, and plan/budget (max_passages, max_seconds, policy flags). Endings node: frontier_ending. (Not: a target ending count.)
- Act count: fixed at 3.
- Countdown: both `{act_remaining}` and `{remaining}`.
- Review cadence: per level, as today.
- RunDock form: plan summary + launch; no duplicate numeric fields.

Follow-up correction, verbatim: "rounding and fractions are adding complexity to something the user could configure directly. let the user select the budgets for each act. premature endings as a result of poor choices should be encouraged, the user will tune the prompts for balance, likely per inference model but that's not our immediate focus"
