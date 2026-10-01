var modal = document.querySelector(".modal");
var modalContent = document.querySelector(".modal-content");

function showContactModal(){
    event.preventDefault();
    modal.style.display = "flex";
}

function closeContactModal(){
    modalContent.classList.add('closing');
}

modalContent.addEventListener('animationend', function() {
    if (modalContent.classList.contains('closing')) {
        modal.style.display = "none";
        modalContent.classList.remove('closing');
    }
});

function openContactLink(link){
    window.open(link, '_blank');
    closeContactModal();
}

modal.addEventListener("click", function (e) {
    const isInsideModalContent = e.target.closest(".modal-content");
    const isInsideButtonContainer = e.target.closest(".button-container");

    if (!isInsideModalContent && !isInsideButtonContainer) {
        closeContactModal();
    }
});