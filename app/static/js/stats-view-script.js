var qrcode = document.getElementById("qrcode");

qrcode.onload = function () {

    var qrcodeContainer = document.getElementById("qr-code-container");
    qrcodeContainer.style.animation = "none";

    qrcodeContainer.addEventListener("mouseenter", function () {
        qrcodeContainer.style.cursor = "pointer";
        var qrcodeOverlay = document.getElementById("qrcode-overlay");
        qrcodeOverlay.style.display = "flex";
    });

    qrcodeContainer.addEventListener("mouseleave", function () {
        const qrcodeOverlay = document.getElementById("qrcode-overlay");
        qrcodeOverlay.style.display = "none";
    });

    qrcodeContainer.addEventListener("click", function () {
        downloadQRCode();
    });

};

function downloadQRCode() {
    fetch(qrcode.src, { mode: "cors" })
        .then(function (res) { return res.blob(); })
        .then(function (blob) {
            var link = document.createElement("a");
            link.href = URL.createObjectURL(blob);
            link.download = "qrcode.png";
            link.click();
            URL.revokeObjectURL(link.href);
        });
}
