#!/bin/bash

export PATH=/home/mk7193/dart-dreamr/flutter/bin:/opt/dreamr-venv/bin:/usr/bin:/home/mk7193/.local/bin:/home/mk7193/bin:/usr/local/bin:/usr/bin:/usr/local/sbin:/usr/sbin

date=$(date "+%Y-%m-%d %H:%M:00")
#mysql -u root -ppay4mysql dreamr -e 'update user_credits set text_remaining_week=2,updated_at = NOW();'
#mysql -u root -ppay4mysql dreamr -e 'update user_credits set free_credits=2,updated_at = NOW();'

mysql -u root -ppay4mysql dreamr <<'SQL'
UPDATE user_credits uc
JOIN users u ON u.id = uc.user_id
SET uc.free_credits = CASE
    WHEN u.signup_date > '2026-05-01' THEN 1
    ELSE 2
END,
uc.updated_at = NOW();
SQL
