// Expert Church Monitoring System - shared front-end behaviour

document.addEventListener("DOMContentLoaded", function () {
  // Auto-show the "birthdays this month" modal on the admin dashboard, if present.
  var birthdayModalEl = document.getElementById("birthdayModal");
  if (birthdayModalEl && window.bootstrap) {
    new bootstrap.Modal(birthdayModalEl).show();
  }

  // Mobile sidebar toggle (the sidebar is off-canvas below ~992px).
  var sidebarToggle = document.getElementById("sidebarToggle");
  var sidebar = document.getElementById("sidebar");
  if (sidebarToggle && sidebar) {
    sidebarToggle.addEventListener("click", function () {
      sidebar.classList.toggle("open");
    });
    document.addEventListener("click", function (e) {
      if (sidebar.classList.contains("open") && !sidebar.contains(e.target) && e.target !== sidebarToggle && !sidebarToggle.contains(e.target)) {
        sidebar.classList.remove("open");
      }
    });
  }
});
