"""AutoHôte scraper — worker Playwright qui pilote Airbnb au nom de l'utilisateur.

Modules :
- db          : connexion Postgres (autonome, lit env vars)
- session     : load/save de la session Playwright (cookies + localStorage)
- selectors   : sélecteurs CSS centralisés pour Airbnb (faciles à patcher)
- airbnb_auth : flow de login passwordless via code email
- airbnb_read : (Phase 3b) lecture des conversations
- airbnb_write: (Phase 3c) envoi de réponses
- worker      : (Phase 3d) boucle qui consume scraper_jobs
"""
