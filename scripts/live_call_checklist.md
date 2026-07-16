# Live-call test checklist

Run each scenario against the live number (or the web-call page). Tick EN / HI /
mixed variants. Reset state between runs where noted. This mirrors how a real
front desk gets tested in production.

## A. Happy-path booking
- [ ] EN: "I'd like to book a physio appointment tomorrow afternoon." → books in ≤ ~7 turns, states branch + practitioner + spoken time, asks full name once, batch-confirms once.
- [ ] HI: "मुझे कल दोपहर की appointment चाहिए।" → same flow fully in Hindi.
- [ ] Mixed: "kal afternoon mein slot hai kya, around four thirty?" → natural Hinglish reply.
- [ ] Verify the booking appears in Cliniko at the stated branch/practitioner/time.

## B. Fuzzy time references (each must trigger a fresh availability search)
- [ ] "Do you have anything on <date> around one?"
- [ ] "Mondays and Wednesdays work well for me."
- [ ] "In the afternoon after I get off work, around four thirty."
- [ ] "Any Thursday morning is great."

## C. Earliest slot across branches
- [ ] "What's the earliest slot available today, anywhere?" → answer must be the true global earliest (verify against `python -m scripts.smoke_cliniko`), naming the branch.

## D. Returning patient
- [ ] Call from a number that booked earlier → greeted by name, no re-asking of known details.

## E. Family shared number
- [ ] Call from +91 90000 00001 (Rahul + Priya Sharma share it in seed data) → agent asks WHO it's for before proceeding, books under the right name. (Use the web page's phone field to simulate.)

## F. Dropped call resume
- [ ] Start a booking, hang up mid-flow after giving name + day preference. Call back within 15 min → agent acknowledges the drop, resumes (does NOT re-ask name/day). 
- [ ] Complete the booking; call again → treated as normal returning patient (no stale resume).

## G. Missed outbound → callback
- [ ] `python -m scripts.outbound_call <your number> "About your Thursday session"` — don't answer. Call the clinic back → agent opens with that context.

## H. Stale availability
- [ ] Ask for Thursday slots; then say "actually what about Friday?" → agent runs a NEW search (listen for the filler phrase again), doesn't answer from memory.
- [ ] Double-booking race: while on a call being offered slot X, book slot X directly in Cliniko dashboard → agent's booking attempt must gracefully offer alternatives, not confirm.

## I. Reschedule & cancel + fee window
- [ ] Reschedule an appointment >24h away → NO fee mentioned.
- [ ] Reschedule/cancel an appointment <24h away → fee of one hundred rupees mentioned.
- [ ] Reschedule books the correct new slot; old slot freed in Cliniko.

## J. Language discipline
- [ ] Speak pure English for 5+ turns → zero Hindi words drift in.
- [ ] Speak pure Hindi → zero unnecessary English drift (clinic terms like "appointment" are fine).
- [ ] Say only "haan" / "OK" mid-English-flow → agent does NOT switch language.
- [ ] Code-switch mid-sentence both directions → agent follows naturally.

## K. Conversational quality
- [ ] Interrupt the agent mid-sentence → it stops, listens, recovers state.
- [ ] During availability lookup → natural holding phrase, no stutter/silence.
- [ ] Times/dates/fees spoken as words ("four thirty", "four hundred rupees").
- [ ] Practitioner names pronounced naturally.

## L. Identity & escalation
- [ ] "Am I talking to a robot?" → honest, graceful, keeps helping.
- [ ] "I want to speak to a human." → logs follow-up, promises callback, does NOT fake a transfer. Verify a row in followup_tickets.
- [ ] Describe a clinical symptom ("my leg is numb, is that serious?") → declines to advise, logs urgent follow-up.

## M. Date/locale correctness
- [ ] "Book me for today" late evening IST → correct local date (no UTC shift).
- [ ] All prices in rupees.
