function validatePassword() {
    var password = document.getElementById("password").value;

    if (password.trim() === "") {
        return true;
    }

    var passwordRegex = /^(?=.*[A-Za-z])(?=.*\d)(?=.*[@.])[A-Za-z\d@.]{8,}$/;
    if (!passwordRegex.test(password)) {
        showNotification("Password must be at least 8 characters long and contain an alphabet, a number, a special character '@' or '.'", 'warning', 10000);
        return false;
    }
    return true;
}

function validateURL() {
    var url = document.getElementById("long-url").value;
    var urlRegex = /^(ftp|http|https):\/\/[^ "]+$/;

    if (!urlRegex.test(url) || url.includes('spoo.me')) {
        showNotification("Please Enter a valid URL", 'warning', 10000);
        return false;
    }
    return true;
}

// Custom Time Expiration is currently Buggy and not ready for Production

// function validateExpiration() {
//     var expiration = document.getElementById("expiration-time").value;

//     if (expiration.trim() === "") {
//         return true;
//     }

//     var expirationDate = new Date(expiration);
//     var currentDate = new Date();
//     var diff = expirationDate - currentDate;
//     console.log(diff);

//     if (diff < 540000) {
//         customTopNotification("ExpirationError", "Expiration date must be at least 10 minutes from now", 10);
//         return false;
//     }

//     return true;
// }
