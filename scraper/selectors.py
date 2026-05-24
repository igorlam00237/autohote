"""Sélecteurs CSS/XPath Airbnb centralisés.

Ces sélecteurs sont les plus susceptibles de casser quand Airbnb refond son UI.
Les concentrer ici permet de tout patcher en un seul endroit.

Conventions :
- LOGIN_*    : page de login et flow auth
- INBOX_*    : page d'inbox messaging
- MESSAGE_*  : éléments d'une conversation
- REPLY_*    : champ et bouton d'envoi

Pour valider/maj : ouvrir Airbnb dans Chrome → F12 → cliquer l'élément.
"""

# === Login passwordless email-code ===

# Page airbnb.fr/login/email
LOGIN_EMAIL_INPUT = 'input[type="email"], input[name="email"], input[id*="email"]'
LOGIN_SUBMIT_EMAIL_BUTTON = 'button[type="submit"]'

# Après saisie email : page de choix méthode (peut proposer SMS/email/etc.)
LOGIN_CHOOSE_EMAIL_BUTTON_TEXT = "Recevoir le code par email"  # ou via 'has-text' xpath

# Page de saisie du code à 6 chiffres
LOGIN_CODE_INPUT = 'input[type="text"][maxlength="6"], input[name*="code"], input[id*="code"]'
LOGIN_CODE_SUBMIT = 'button[type="submit"]'

# Élément qui prouve qu'on est connecté (avatar host visible en haut à droite)
LOGGED_IN_INDICATOR = '[data-testid="layout-host-header"], a[href*="/hosting"]'

# === Inbox / conversation (à remplir Phase 3b) ===
# INBOX_LIST_CONTAINER = ...
# MESSAGE_BUBBLES = ...

# === Reply (à remplir Phase 3c) ===
# REPLY_TEXTAREA = ...
# REPLY_SEND_BUTTON = ...
