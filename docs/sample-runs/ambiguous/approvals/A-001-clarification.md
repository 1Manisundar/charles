Node: Requirements analysis
Why a human is needed: The requirement is ambiguous; a human must confirm how it is interpreted.

Interpretation so far: The request names three symptoms - load ('getting hammered'), bad destinations ('dodgy links') and a wish for visitor insight - without saying what should change. Each has several plausible readings with very different cost and risk, and the third touches personal data. The requirements below are the reading the analyst recommends; they must not be built until a product owner has confirmed the three open questions.

Q1: What does 'getting hammered' mean we should do?
  why it matters: Throttling abusive clients, caching hot redirects and scaling out are different projects. Only the first is a code change of bounded size; picking wrong either leaves the problem or builds infrastructure nobody asked for.
  recommended: Rate-limit each client (stricter for link creation than for redirects) and answer 429 when exceeded

Q2: What makes a link 'dodgy', and how should it be stopped?
  why it matters: Blocking by rule is cheap and has no external dependency; a reputation service catches real phishing but adds a vendor, latency, cost and a new failure mode on the create path.
  recommended: Refuse destinations that are internal or private addresses, disguised with credentials, pointing back at the service, or on a configurable blocklist

Q3: What does 'who is clicking' need to reveal?
  why it matters: Storing IP addresses or user agents makes the click log personal data, with consent, retention and breach obligations. Aggregate measures answer most product questions without that liability.
  recommended: Aggregates only: unique visitors per day and top referring sites, with nothing stored that identifies a person
