(function () {
    const current = window.location.pathname;
    document.querySelectorAll('.nav-link').forEach((link) => {
        const href = link.getAttribute('href');
        if (href && current === href) {
            link.style.background = 'rgba(255,255,255,.06)';
            link.style.borderRadius = '14px';
        }
    });
})();
