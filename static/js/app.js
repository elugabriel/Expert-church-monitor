// Expert Church Monitoring System - shared front-end behaviour

document.addEventListener("DOMContentLoaded", function () {
  // Auto-show the "birthdays this month" modal on the admin dashboard, if present.
  var birthdayModalEl = document.getElementById("birthdayModal");
  if (birthdayModalEl && window.bootstrap) {
    new bootstrap.Modal(birthdayModalEl).show();
  }
});
