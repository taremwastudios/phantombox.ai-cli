/**
 * PhantomBox Identity — frontend client
 *
 * Vanilla JavaScript. Uses the EXISTING Core JSON contract; no new
 * endpoints, no direct database access, no invented responses.
 *
 *   POST /v1/identity/email/request      { email }
 *   POST /v1/identity/email/verify       { email, code }
 *   POST /v1/identity/profile/complete   { first_name, last_name, phone_number }  (Bearer)
 *   POST /v1/identity/cli/authorization/approve | deny  { reference }             (Bearer)
 *
 * The session token is kept in sessionStorage only for the duration of the
 * sign-in tab; it is never written to localStorage and never rendered.
 */
(function () {
    'use strict';

    var STORAGE_KEY = 'pb_identity_session';
    var CSRF = (document.querySelector('meta[name="csrf-token"]') || {}).content || '';

    // Only allow same-site, absolute-path redirect targets.
    function safeReturnTo(value) {
        if (typeof value !== 'string' || value === '') return '';
        if (value.charAt(0) !== '/' || value.charAt(1) === '/' || value.charAt(1) === '\\') return '';
        return value;
    }

    var returnTo = safeReturnTo(new URLSearchParams(window.location.search).get('return_to'));

    // ---------------------------------------------------------------
    // Small helpers
    // ---------------------------------------------------------------
    function $(id) { return document.getElementById(id); }

    function setLoading(button, loading) {
        if (!button) return;
        var text = button.querySelector('.btn-text');
        var spinner = button.querySelector('.btn-spinner');
        button.disabled = loading;
        if (text && spinner) {
            text.style.visibility = loading ? 'hidden' : 'visible';
            spinner.classList.toggle('hidden', !loading);
        }
    }

    /**
     * POST JSON to the Core API.
     *
     * Resolves to { ok, status, data } where `ok` mirrors the HTTP result.
     * The server's own body is always returned untouched so the caller can
     * display exactly what it said.
     */
    function post(path, payload, token) {
        var headers = {
            'Content-Type': 'application/json',
            'Accept': 'application/json',
            'X-PhantomBox-Client': 'web'
        };
        if (CSRF) headers['X-CSRF-Token'] = CSRF;
        if (token) headers['Authorization'] = 'Bearer ' + token;

        return fetch(path, {
            method: 'POST',
            credentials: 'same-origin',
            headers: headers,
            body: JSON.stringify(payload || {})
        }).then(function (res) {
            return res.json().catch(function () { return null; }).then(function (data) {
                return { ok: res.ok, status: res.status, data: data };
            });
        });
    }

    /** Message to display for a failed response — the server's text if any. */
    function errorText(result, fallback) {
        var d = result && result.data;
        if (d && typeof d === 'object') {
            if (typeof d.error === 'string' && d.error) return d.error;
            if (typeof d.message === 'string' && d.message) return d.message;
        }
        if (result && result.status) return fallback + ' (HTTP ' + result.status + ')';
        return fallback;
    }

    function showAlert(el, textEl, message, isError) {
        if (!el) return;
        el.classList.remove('hidden', 'alert-error', 'alert-success');
        el.classList.add(isError ? 'alert-error' : 'alert-success');
        if (textEl) textEl.textContent = message;
    }

    function hideAlert(el) { if (el) el.classList.add('hidden'); }

    // Session token: sessionStorage only (cleared when the tab closes).
    function saveSession(token) {
        try { window.sessionStorage.setItem(STORAGE_KEY, token); } catch (e) { /* storage blocked */ }
    }
    function loadSession() {
        try { return window.sessionStorage.getItem(STORAGE_KEY) || ''; } catch (e) { return ''; }
    }
    function clearSession() {
        try { window.sessionStorage.removeItem(STORAGE_KEY); } catch (e) { /* ignore */ }
    }



    // =================================================================
    // Sign-in page: email identification → OTP → profile completion
    // =================================================================
    var requestForm = $('requestOtpForm');

    if (requestForm) {
        var verifyForm = $('verifyOtpForm');
        var profileForm = $('completeProfileForm');
        var emailInput = $('emailInput');
        var codeInput = $('codeInput');
        var emailTarget = $('displayEmailTarget');
        var alertBox = $('signInAlert');
        var alertText = $('signInAlertText');
        var subtitle = $('authSubtitle');
        var cardTitle = $('authTitle');

        // Rendering the "new account" path is server-decided: the layout is
        // switched by the data-flow attribute set from the Core response.
        var dots = [$('stepDot1'), $('stepDot2'), $('stepDot3')];
        var captions = [$('stepCaption1'), $('stepCaption2'), $('stepCaption3')];

        // The Core decides whether an email belongs to an existing account: it
        // answers `profile_required` from the profile state of the user it
        // resolved for that address. Until that answer arrives the UI makes no
        // claim about which kind of account this is.
        function setFlow(kind) {
            if (kind !== 'register') {
                // The response did not confirm a new account, so the neutral
                // sign-in copy stays as rendered by the template.
                return;
            }

            if (cardTitle) cardTitle.textContent = 'Create your PhantomBox account';
            if (captions[2]) captions[2].textContent = 'Profile';

            var kicker = $('authKicker');
            if (kicker) kicker.textContent = 'New account';
        }

        function setStep(n) {
            for (var i = 0; i < 3; i++) {
                var dot = dots[i];
                var caption = captions[i];
                if (dot) {
                    dot.classList.remove('active', 'done');
                    if (i + 1 < n) dot.classList.add('done');
                    if (i + 1 === n) {
                        dot.classList.add('active');
                        dot.setAttribute('aria-current', 'step');
                    } else {
                        dot.removeAttribute('aria-current');
                    }
                }
                // Only the current step's caption is shown.
                if (caption) caption.classList.toggle('hidden', i + 1 !== n);
            }
        }

        function showVerifyStep() {
            requestForm.classList.add('hidden');
            profileForm.classList.add('hidden');
            verifyForm.classList.remove('hidden');
            setStep(2);
            if (subtitle) subtitle.textContent = 'Check your email for the 6-digit confirmation code.';
            if (codeInput) codeInput.focus();
        }

        function showProfileStep() {
            requestForm.classList.add('hidden');
            verifyForm.classList.add('hidden');
            profileForm.classList.remove('hidden');
            setStep(3);
            if (subtitle) subtitle.textContent = 'One last step: your account needs a few details.';
            var first = $('firstNameInput');
            if (first) first.focus();
        }

        // ---------------------------------------------------------
        // Step 1 — identify the email and request a code
        // ---------------------------------------------------------
        requestForm.addEventListener('submit', function (event) {
            event.preventDefault();
            hideAlert(alertBox);

            var email = (emailInput.value || '').trim();
            if (!email || email.indexOf('@') < 1) {
                showAlert(alertBox, alertText, 'Please enter a valid email address.', true);
                emailInput.focus();
                return;
            }

            var button = $('btnRequestOtp');
            setLoading(button, true);

            post('/v1/identity/email/request', { email: email }).then(function (result) {
                setLoading(button, false);

                if (!result.ok || !result.data || result.data.ok !== true) {
                    // Server rejected it: show exactly why (validation, rate
                    // limit, cooldown, delivery failure). Never claim success.
                    showAlert(alertBox, alertText, errorText(result, 'Could not send the verification code.'), true);
                    return;
                }

                // The server tells us whether this is a new account.
                // `profile_required` is only present when the account still
                // needs first/last name and phone.
                var needsProfile = result.data.profile_required === true;
                setFlow(needsProfile ? 'register' : 'signin');

                emailTarget.textContent = email;
                showVerifyStep();
            }).catch(function () {
                setLoading(button, false);
                showAlert(alertBox, alertText, 'Network error — could not reach the Identity service.', true);
            });
        });

        // ---------------------------------------------------------
        // Step 2 — verify the code
        // ---------------------------------------------------------
        verifyForm.addEventListener('submit', function (event) {
            event.preventDefault();
            hideAlert(alertBox);

            var code = (codeInput.value || '').trim();
            if (code.length < 4) {
                showAlert(alertBox, alertText, 'Please enter the verification code from your email.', true);
                codeInput.focus();
                return;
            }

            var button = $('btnVerifyOtp');
            setLoading(button, true);

            var email = (emailInput.value || '').trim();

            post('/v1/identity/email/verify', { email: email, code: code }).then(function (result) {
                setLoading(button, false);

                if (!result.ok || !result.data || result.data.ok !== true) {
                    showAlert(alertBox, alertText, errorText(result, 'Verification failed.'), true);
                    return;
                }

                // A real session token is only ever minted by the server.
                if (result.data.token) saveSession(result.data.token);

                if (result.data.status === 'profile_required') {
                    setFlow('register');
                    showProfileStep();
                    return;
                }

                // Existing, complete account — the flow is finished.
                setStep(3);
                window.location.href = returnTo || result.data.redirect || '/';
                return;
            }).catch(function () {
                setLoading(button, false);
                showAlert(alertBox, alertText, 'Network error — could not verify the code.', true);
            });
        });

        // ---------------------------------------------------------
        // Step 3 — complete the profile (new accounts only)
        // ---------------------------------------------------------
        profileForm.addEventListener('submit', function (event) {
            event.preventDefault();
            hideAlert(alertBox);

            var firstName = ($('firstNameInput').value || '').trim();
            var lastName = ($('lastNameInput').value || '').trim();
            var phone = ($('phoneInput').value || '').trim();

            var token = loadSession();
            if (!token) {
                showAlert(alertBox, alertText, 'Your sign-in session has expired. Please start again.', true);
                return;
            }

            if (!firstName || !lastName) {
                showAlert(alertBox, alertText, 'Please provide both first and last name.', true);
                return;
            }
            if (!phone) {
                showAlert(alertBox, alertText, 'Please enter your phone number with country code (e.g. +256…).', true);
                return;
            }

            var button = $('btnCompleteProfile');
            setLoading(button, true);

            post('/v1/identity/profile/complete', {
                first_name: firstName,
                last_name: lastName,
                phone_number: phone
            }, token).then(function (result) {
                setLoading(button, false);

                if (!result.ok || !result.data || result.data.ok !== true) {
                    showAlert(alertBox, alertText, errorText(result, 'Profile update failed.'), true);
                    if (result.status === 401) clearSession();
                    return;
                }

                window.location.href = returnTo || result.data.redirect || '/';
            }).catch(function () {
                setLoading(button, false);
                showAlert(alertBox, alertText, 'Network error — could not save your profile.', true);
            });
        });

        // Change email
        var changeBtn = $('btnChangeEmail');
        if (changeBtn) {
            changeBtn.addEventListener('click', function () {
                hideAlert(alertBox);
                verifyForm.classList.add('hidden');
                profileForm.classList.add('hidden');
                requestForm.classList.remove('hidden');
                setStep(1);
                setFlow(null);
                emailInput.focus();
            });
        }

        // Resend code — reports the server's real answer (incl. cooldown)
        var resendBtn = $('btnResendOtp');
        if (resendBtn) {
            resendBtn.addEventListener('click', function () {
                hideAlert(alertBox);
                var email = (emailInput.value || '').trim();
                if (!email) return;

                resendBtn.disabled = true;
                resendBtn.textContent = 'Resending…';

                post('/v1/identity/email/request', { email: email }).then(function (result) {
                    if (result.ok && result.data && result.data.ok === true) {
                        showAlert(alertBox, alertText,
                            result.data.message || 'A new verification code has been sent.', false);
                    } else {
                        showAlert(alertBox, alertText, errorText(result, 'Could not resend the code.'), true);
                    }
                }).catch(function () {
                    showAlert(alertBox, alertText, 'Network error — could not resend the code.', true);
                }).then(function () {
                    resendBtn.disabled = false;
                    resendBtn.textContent = 'Resend code';
                });
            });
        }

        // Digits only, auto-submit once six are typed
        codeInput.addEventListener('input', function () {
            codeInput.value = codeInput.value.replace(/[^0-9a-z]/gi, '').toUpperCase();
            if (codeInput.value.length === 6) verifyForm.requestSubmit();
        });

        if (emailInput.value) {
            emailInput.select();
        }
    }

    // =================================================================
    // CLI device authorization page
    // =================================================================
    var approveBtn = $('btnApproveDevice');

    if (approveBtn) {
        // An approval requires a real, signed-in browser session. Without one,
        // go to sign-in and return to this exact authorization link.
        var deviceReturnTo = approveBtn.getAttribute('data-return-to') || '';
        if (!loadSession()) {
            window.location.replace('/sign-in?return_to=' + encodeURIComponent(deviceReturnTo));
            return;
        }

        var denyBtn = $('btnDenyDevice');
        var deviceAlert = $('deviceAlert');
        var deviceText = $('deviceAlertText');
        var deviceActions = $('deviceActions');
        var completion = $('completionState');
        var completionTitle = $('completionTitle');
        var completionDesc = $('completionDesc');

        function showCompletion(title, desc, approved) {
            if (deviceActions) deviceActions.classList.add('hidden');
            hideAlert(deviceAlert);
            if (completion) {
                completion.classList.remove('hidden', 'approved', 'denied');
                completion.classList.add(approved ? 'approved' : 'denied');
            }
            if (completionTitle) completionTitle.textContent = title;
            if (completionDesc) completionDesc.textContent = desc;
        }

        function decide(action) {
            var reference = approveBtn.getAttribute('data-reference') || '';
            var token = loadSession();

            if (!token) {
                showAlert(deviceAlert, deviceText,
                    'You are not signed in. Sign in, then reopen this authorization link.', true);
                return;
            }

            var button = action === 'approve' ? approveBtn : denyBtn;
            setLoading(button, true);
            if (button === approveBtn && denyBtn) denyBtn.disabled = true;
            if (button === denyBtn && approveBtn) approveBtn.disabled = true;

            post('/v1/identity/cli/authorization/' + action,
                { reference: reference }, token).then(function (result) {
                    if (result.ok && result.data && result.data.ok === true) {
                        showCompletion(
                            result.data.status === 'approved'
                                ? 'Device authorization granted'
                                : 'Device authorization denied',
                            result.data.message || 'You can close this tab and return to your terminal.',
                            result.data.status === 'approved'
                        );
                        return;
                    }

                    setLoading(button, false);
                    if (approveBtn) approveBtn.disabled = false;
                    if (denyBtn) denyBtn.disabled = false;

                    if (result.status === 401) {
                        showAlert(deviceAlert, deviceText,
                            'Your session has expired. Please sign in again.', true);
                        return;
                    }

                    showAlert(deviceAlert, deviceText,
                        errorText(result, 'The Core API could not process this request.'), true);
                }).catch(function () {
                    setLoading(button, false);
                    if (approveBtn) approveBtn.disabled = false;
                    if (denyBtn) denyBtn.disabled = false;
                    showAlert(deviceAlert, deviceText,
                        'Network error — could not reach the Identity service.', true);
                });
        }

        approveBtn.addEventListener('click', function () { decide('approve'); });
        if (denyBtn) denyBtn.addEventListener('click', function () { decide('deny'); });
    }
})();
