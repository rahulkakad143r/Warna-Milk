document.addEventListener("DOMContentLoaded", function () {

    const password = document.getElementById("id_password");
    const eyeIcon = document.getElementById("eyeIcon");

    /* ================= PASSWORD SHOW/HIDE ================= */

    if (eyeIcon && password) {

        eyeIcon.addEventListener("click", function () {

            if (password.type === "password") {

                password.setAttribute("aria-label", "Hide password");
                password.type = "text";

                eyeIcon.classList.remove("fa-eye");
                eyeIcon.classList.add("fa-eye-slash");

            } else {

                password.setAttribute("aria-label", "Show password");
                password.type = "password";

                eyeIcon.classList.remove("fa-eye-slash");
                eyeIcon.classList.add("fa-eye");
            }

        });


        eyeIcon.addEventListener("keydown", function (event) {

            if (event.key === "Enter" || event.key === " ") {

                event.preventDefault();
                eyeIcon.click();
            }

        });
    }


    /* ============ FORGOT LINK / RESET POPUP (2 STEPS) ============ */

    const forgotLink = document.getElementById("forgotLink");
    const resetPopup = document.getElementById("resetPopup");
    const resetPopupClose = document.getElementById("resetPopupClose");
    const step1 = document.getElementById("resetStep1");
    const step2 = document.getElementById("resetStep2");
    const checkForm = document.getElementById("resetCheckForm");
    const checkError = document.getElementById("checkError");
    const checkSubmit = document.getElementById("checkSubmit");
    const resetForm = document.getElementById("resetForm");
    const resetError = document.getElementById("resetError");
    const resetSubmit = document.getElementById("resetSubmit");
    const resetBackBtn = document.getElementById("resetBackBtn");
    const successPopup = document.getElementById("successPopup");
    const successMessage = document.getElementById("successPopupMessage");
    const successLoginBtn = document.getElementById("successLoginBtn");

    function showPopup(popup) {
        if (popup) {
            popup.hidden = false;
            document.body.classList.add("popup-open");
        }
    }

    function hidePopup(popup) {
        if (popup) {
            popup.hidden = true;
        }
        if ((!resetPopup || resetPopup.hidden) &&
            (!successPopup || successPopup.hidden)) {
            document.body.classList.remove("popup-open");
        }
    }

    function showError(box, message) {
        if (!box) return;
        box.textContent = message;
        box.hidden = false;
    }

    function clearError(box) {
        if (!box) return;
        box.textContent = "";
        box.hidden = true;
    }

    function showStep(which) {
        if (step1) step1.hidden = which !== 1;
        if (step2) step2.hidden = which !== 2;
    }

    function openResetPopup() {
        clearError(checkError);
        clearError(resetError);
        if (checkForm) checkForm.reset();
        if (resetForm) resetForm.reset();
        showStep(1);
        showPopup(resetPopup);
        const first = document.getElementById("reset_username");
        if (first) first.focus();
    }

    function postForm(url, form) {
        return fetch(url, {
            method: "POST",
            body: new FormData(form),
            headers: {"X-Requested-With": "XMLHttpRequest"}
        }).then(function (response) {
            return response.json().then(function (data) {
                return {ok: response.ok, data: data};
            });
        });
    }

    if (forgotLink) {
        forgotLink.addEventListener("click", function (event) {
            event.preventDefault();
            openResetPopup();
        });
    }

    if (resetPopupClose) {
        resetPopupClose.addEventListener("click", function () {
            hidePopup(resetPopup);
        });
    }

    if (resetPopup) {
        resetPopup.addEventListener("click", function (event) {
            if (event.target === resetPopup) hidePopup(resetPopup);
        });
    }

    document.addEventListener("keydown", function (event) {
        if (event.key !== "Escape") return;
        if (resetPopup && !resetPopup.hidden) hidePopup(resetPopup);
    });


    /* ---------- step 1: check the username in the database ---------- */

    if (checkForm) {
        checkForm.addEventListener("submit", function (event) {
            event.preventDefault();
            clearError(checkError);

            const username = document.getElementById("reset_username").value.trim();
            if (!username) {
                showError(checkError, "Please enter username to check.");
                document.getElementById("reset_username").focus();
                return;
            }

            checkSubmit.disabled = true;

            postForm(checkForm.action, checkForm)
                .then(function (result) {
                    checkSubmit.disabled = false;

                    if (!result.ok) {
                        showError(checkError, result.data.error || "Username not found.");
                        return;
                    }

                    /* username exists in the database -> open step 2 */
                    const verified = result.data.username || username;
                    document.getElementById("reset_account").value = verified;
                    document.getElementById("reset_new_username").value = verified;
                    document.getElementById("verifiedNote").textContent =
                        result.data.message || "Username verified in the database.";

                    showStep(2);
                    document.getElementById("reset_new_password").focus();
                })
                .catch(function () {
                    checkSubmit.disabled = false;
                    showError(checkError, "Something went wrong. Please try again.");
                });
        });
    }


    /* ---------- step 2: save username + password to the database ---------- */

    if (resetBackBtn) {
        resetBackBtn.addEventListener("click", function () {
            clearError(resetError);
            showStep(1);
            document.getElementById("reset_username").focus();
        });
    }

    if (resetForm) {
        resetForm.addEventListener("submit", function (event) {
            event.preventDefault();
            clearError(resetError);

            const newPassword = document.getElementById("reset_new_password").value;
            const confirmPassword = document.getElementById("reset_confirm_password").value;

            if (!newPassword) {
                showError(resetError, "Please enter a new password.");
                document.getElementById("reset_new_password").focus();
                return;
            }
            if (newPassword !== confirmPassword) {
                showError(resetError, "The two passwords do not match.");
                document.getElementById("reset_confirm_password").focus();
                return;
            }

            resetSubmit.disabled = true;

            postForm(resetForm.action, resetForm)
                .then(function (result) {
                    resetSubmit.disabled = false;

                    if (!result.ok) {
                        showError(resetError, result.data.error || "Could not update the database.");
                        return;
                    }

                    /* username + password are stored in the database */
                    if (successMessage && result.data.message) {
                        successMessage.textContent = result.data.message;
                    }
                    hidePopup(resetPopup);
                    showPopup(successPopup);
                })
                .catch(function () {
                    resetSubmit.disabled = false;
                    showError(resetError, "Something went wrong. Please try again.");
                });
        });
    }

    if (successLoginBtn) {
        successLoginBtn.addEventListener("click", function () {
            hidePopup(successPopup);

            /* back to the login popup: fresh password box, username focused */
            const passwordField = document.getElementById("id_password");
            const usernameField = document.getElementById("id_username");
            if (passwordField) passwordField.value = "";
            if (usernameField) usernameField.focus();
        });
    }


    /* ================= LOGIN VALIDATION ================= */

    const loginForm = document.getElementById("loginForm");

    if (loginForm) {

        loginForm.addEventListener("submit", function (event) {

            const usernameField = document.getElementById("id_username");
            const passwordField = document.getElementById("id_password");

            const username = usernameField.value.trim();
            const pass = passwordField.value.trim();

            if (username === "") {

                event.preventDefault();

                alert("Please enter username.");
                usernameField.focus();

                return;
            }

            if (pass === "") {

                event.preventDefault();

                alert("Please enter password.");
                passwordField.focus();

                return;
            }

        });
    }

});
