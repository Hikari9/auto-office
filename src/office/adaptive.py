"""Adaptive executor/worker recommendation (#300).

`routing.route` qualifies candidates (hard exclusions, trust, capabilities,
floor, task shape, quota). For executor and worker routes it then hands the
qualifying set here, which compares them and returns a slate: a primary and up
to two fallbacks, each with a reason, a strength and a weakness, plus the full
per-candidate evidence for `office inspect route`.

Scoring is offline and reproducible: every input arrives in the request (the
candidates with their probed quota, the learner's evidence snapshot, the
routing seed and the exploration history), so the same request yields the same
decision. The objective is expected cost to a successful task plus speed, not
raw token price:

    p_success     Beta posterior; the benchmark prior is worth `prior_strength`
                  pseudo-outcomes, so comparable local evidence takes over as it grows
    attempt cost  catalog price x effort token multiplier (prior), or local spend
    cost to success  attempt cost x (1 + rework per review round) / p_success
    time to success  attempt wall x (1 + rework per review round) / p_success
    utility       weighted sum of effectiveness, cost, speed, quota headroom and
                  user preference, minus a small spread penalty

Candidates within the competitive band of the best utility are a close call:
a reproducible draw from the routing seed picks among them. Exploration, at a
small bounded rate, may promote an under-tested qualify¶»§q«^u±Í”˜‰•áÁ•Ñ•½ÍĞ½Ù•ÈÑ¡”€‘í™±½…Ğ¡•¥±¥¹œ¤éô‰Õ‘•Ğ•¥±¥¹œˆ¥˜¹½Ğ•¥±¥¹}½¬•±Í”9½¹”¤(€€€€€€€½ÕÑm…¹‘¥‘…Ñ•}¥¡Œ¥t€ôì‰É½Üˆèì¨©È°€‰½µÁ½¹•¹ÑÌˆè½µÁÌ°€‰ÕÑ¥±¥ÑäˆèÕÑ¥±¥Ñåô°€‰ÕÑ¥±¥ÑäˆèÕÑ¥±¥Ñä°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰İ¥Ñ¡¥¹}µ…É¥¸ˆèµ…É¥¹}½¬°€‰İ¥Ñ¡¥¹}½ÍĞˆè½ÍÑ}½¬°€‰İ¥Ñ¡¥¹}•¥±¥¹œˆè•¥±¥¹}½¬°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰É•…Í½¸ˆèÉ•…Í½¹ô(€€€É•ÑÕÉ¸½ÕĞ(()‘•˜}Ñ…Í­}‘•ÍÉ¥ÁÑ½È¡É•ÅÕ•ÍĞè‘¥Ğ¤€´ø‘¥Ğè(€€€€ˆˆ‰Q¡”Ñ…Í¬‘•ÍÉ¥ÁÑ½ÈÍ½É¥¹œÕÍ•èÉ•ÅÕ•ÍĞ½¹Ñ•áĞ™¥ÉÍĞ°•±Í”Ñ¡”•Ù¥‘•¹”½¹Ñ•áĞ¸ˆˆˆ(€€€É•ÑÕÉ¸€ ¡É•ÅÕ•ÍĞ¹•Ğ ‰½¹Ñ•áĞˆ¤½È€¡É•ÅÕ•ÍĞ¹•Ğ ‰•Ù¥‘•¹”ˆ¤½Èíô¤¹•Ğ ‰½¹Ñ•áĞˆ¤½Èíô¤(€€€€€€€€€€€€¹•Ğ ‰Ñ…Í­}‘•ÍÉ¥ÁÑ½Èˆ¤½Èíô¤(()‘•˜}‘•‘ÕÁ”¡…¹‘¥‘…Ñ•Ìè±¥ÍÑm‘¥Ñt¤€´øÑÕÁ±•m±¥ÍÑm‘¥Ñt°‘¥ÑmÍÑÈ°ÍÑÉutè(€€€€ˆˆ‰=¹”É½ÜÁ•È¥¹Ù½…Ñ¥½¸è…¸…±¥…Ì€¡Í½¹¹•Ñ€¤…¹Ñ¡”½¹É•Ñ”µ½‘•°¥Ğ(€€€É•Í½±Ù•ÌÑ¼…É”Ñ¡”Í…µ”É½ÕÑ”¸Q¡”½¹É•Ñ”É½Ü¥Ì­•ÁĞ¸ˆˆˆ(€€€™É½´½™™¥”¹É½ÕÑ¥¹œ¥µÁ½ÉĞ…¹‘¥‘…Ñ•}¥(€€€­••Àè‘¥ÑmÍÑÈ°‘¥Ñt€ôíô(€€€…±¥…Í•Ìè‘¥ÑmÍÑÈ°ÍÑÉt€ôíô(€€€™½ÈŒ¥¸…¹‘¥‘…Ñ•Ìè(€€€€€€€­•ä€ôÉ½ÕÑ•}±•…É¹¥¹œ¹…¹‘¥‘…Ñ•}­•ä¡Œ¤(€€€€€€€ÕÈ€ô­••À¹•Ğ¡­•ä¤(€€€€€€€½¹É•Ñ”€ôŒ¹•Ğ ‰µ½‘•±}¥ˆ¤€ôôŒ¹•Ğ ‰¥¹Ù½…Ñ¥½¹}µ½‘•±}¥ˆ¤(€€€€€€€¥˜ÕÈ¥Ì9½¹”è(€€€€€€€€€€€­••Ám­•åt€ôŒ(€€€€€€€•±¥˜½¹É•Ñ”…¹ÕÈ¹•Ğ ‰µ½‘•±}¥ˆ¤€„ôÕÈ¹•Ğ ‰¥¹Ù½…Ñ¥½¹}µ½‘•±}¥ˆ¤è(€€€€€€€€€€€…±¥…Í•Ím…¹‘¥‘…Ñ•}¥¡ÕÈ¥t€ô…¹‘¥‘…Ñ•}¥¡Œ¤(€€€€€€€€€€€­••Ám­•åt€ôŒ(€€€€€€€•±Í”è(€€€€€€€€€€€…±¥…Í•Ím…¹‘¥‘…Ñ•}¥¡Œ¥t€ô…¹‘¥‘…Ñ•}¥¡ÕÈ¤(€€€É•ÑÕÉ¸mŒ™½ÈŒ¥¸…¹‘¥‘…Ñ•Ì¥˜­••À¹•Ğ¡É½ÕÑ•}±•…É¹¥¹œ¹…ºÚîÆ­y