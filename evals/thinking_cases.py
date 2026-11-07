"""Thirty balanced ChatML-style held-out prompts for thinking-mode evaluation."""

SYSTEM_PROMPT = (
    "Classify whether the user message needs deliberate reasoning. "
    'Reply only with {"thinking_mode": true} or {"thinking_mode": false}.'
)

# Each tuple is (user message, expected thinking_mode). The runner sends these
# as system/user ChatML messages and compares the model's assistant decision.
CASES = [
    ("Hi, how are you?", False),
    ("What is the capital of Japan?", False),
    ("Rewrite this sentence to be more concise: 'Due to the fact that we were late, we missed the opening.'", False),
    ("Translate 'The train leaves at noon' into French.", False),
    ("Extract the email address from this text: Contact maya@example.com for details.", False),
    ("Return only JSON with the key name and value Danica.", False),
    ("Sort these names alphabetically: Zoe, Amir, Lin.", False),
    ("What is 17 times 24? Reply with only the number.", False),
    ("Convert 3 kilometers to meters.", False),
    ("Give me one synonym for 'careful'.", False),
    ("How do I list files in the current PowerShell directory?", False),
    ("Summarize this in one sentence: The shop opens at nine, closes at five, and is closed on Sundays.", False),
    ("Change this to lowercase: HELLO WORLD.", False),
    ("What day comes after Tuesday?", False),
    ("Write a polite one-line thank-you for a quick reply.", False),
    ("Okay, Dimitri is 2 times as old as Wei. In 4 years the sum of their ages will be 49. How old is each now?", True),
    ("Alice is taller than Bob, Bob is taller than Cara, and Dan is shorter than Cara. Who is tallest and who is shortest?", True),
    ("Compare polling and event-driven updates for this app, explain the trade-offs, and recommend one given low traffic and strict latency needs.", True),
    ("Investigate why this test passes alone but fails in the full suite. Give likely causes and a step-by-step way to isolate the issue.", True),
    ("Plan a database migration with dependencies, a rollback path, and checks that avoid downtime.", True),
    ("A project has a fixed budget, three required features, and two engineers available for four weeks. Propose a feasible schedule and explain what must be cut if it does not fit.", True),
    ("Analyze these quarterly values for trend and anomalies: 12, 14, 13, 30, 15, 17. Explain whether the spike changes the overall conclusion.", True),
    ("Synthesize the conflicting recommendations in these two reports and identify which evidence supports each conclusion.", True),
    ("Derive a formula for the break-even quantity when fixed costs are 1200, price per unit is 40, and variable cost per unit is 25.", True),
    ("Assess the security and reliability risks of storing refresh tokens in browser local storage, then recommend mitigations.", True),
    ("Design a two-day itinerary that satisfies these constraints: no car, one museum each day, lunch near each museum, and total walking below 8 km per day.", True),
    ("Review this function for edge cases, identify the most consequential bug, and propose a minimal fix with a regression test.", True),
    ("Given that all approved requests are logged, some logged requests are later rejected, and this request was rejected, can we conclude it was approved? Explain the logic.", True),
    ("Compare the total cost of these plans over three years, including the setup fee, monthly charge, and the stated annual increase, then recommend the cheaper option.", True),
    ("Turn these requirements into an implementation sequence, identify blockers, and explain how you would verify each milestone.", True),
]

