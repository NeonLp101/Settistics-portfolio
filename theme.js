// Applies the saved color theme before the page paints (kept separate: the CSP allows no inline scripts).
try{if(localStorage.getItem('settistics-theme')==='void')document.documentElement.dataset.palette='void';}catch{}
