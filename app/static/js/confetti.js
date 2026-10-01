var amount = 200,
    between = (min, max) => min + Math.random() * (max - min),
    colors = [
        "#d400ff",
        "#067df7",
        "#ff0080",
        "#F3722C",
        "#F8961E",
        "#F9C74F",
        "#90BE6D",
        "#43AA8B",
        "#577590"
    ],
    current = 0;

let interval = setInterval(() => {
    if (current < amount) {
        animate(createConfetti());
    }
}, 30);

setTimeout(() => clearInterval(interval), 1200);

function createConfetti() {
    let div = document.createElement('div');
    // Randomize the size and shape of the confetti
    let size = between(6, 12);
    let shape = Math.random() > 0.5 ? 'circle' : 'square';
    gsap.set(div, {
        attr: {
            class: 'confetti',
            style: '--color: ' + colors[Math.floor(Math.random() * colors.length)]
        },
        x: between(0, window.innerWidth),
        y: -window.innerHeight / 4,
        z: between(-80, 80),
        width: size,
        height: size,
        borderRadius: shape === 'circle' ? '50%' : '0%'
    });
    current++;
    document.body.appendChild(div);
    return div;
}

function animate(element) {
    let rotationZ = between(0, 360);
    gsap.to(element, {
        y: window.innerHeight + 40,
        ease: 'power1.out',
        delay: between(0, .15),
        duration: between(1.5, 3),
        onComplete() {
            if (element instanceof Element || element instanceof HTMLDocument) {
                current--;
                element.remove();
            }
        }
    });
    gsap.to(element, {
        rotationZ: rotationZ + between(90, 180),
        duration: between(2, 4),
        ease: 'none'
    });
}
